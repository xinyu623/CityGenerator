"""Overpass API client with disk cache, throttling, chunking, and retries.

Selections larger than ``overpass_chunk_area_km2`` are fetched as a grid of
smaller requests (at most ``sqrt(area)`` km per side), cached per cell, and
merged with duplicate elements removed. That keeps any single request small
enough for a busy public endpoint while preserving the exact footprint set of
one big query.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.models.schemas import BBox
from app.services.errors import OverpassError
from app.services.io_utils import (
    is_older_than,
    read_json,
    sha256_text,
    utc_now_iso,
    write_json_atomic,
)

logger = logging.getLogger(__name__)

QUERY_VERSION = "buildings-v1"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
_M_PER_DEG_LAT = 110_900.0
_M_PER_DEG_LON_EQUATOR = 111_320.0


def _extent_m(bbox: BBox) -> tuple[float, float]:
    """Approximate the bbox's local width and height in metres."""

    mid_lat = (bbox.south + bbox.north) / 2.0
    height_m = (bbox.north - bbox.south) * _M_PER_DEG_LAT
    width_m = (
        (bbox.east - bbox.west)
        * _M_PER_DEG_LON_EQUATOR
        * max(math.cos(math.radians(mid_lat)), 0.01)
    )
    return width_m, height_m


def _planar_area_km2(bbox: BBox) -> float:
    """Area used by the chunking grid, in km² (not the ellipsoid area)."""

    width_m, height_m = _extent_m(bbox)
    return abs(width_m * height_m) / 1_000_000.0


def quarter_bboxes(bbox: BBox) -> list[BBox]:
    """Cut ``bbox`` into four equal quarters at its mid-latitude/longitude."""

    mid_lat = (bbox.south + bbox.north) / 2.0
    mid_lon = (bbox.west + bbox.east) / 2.0
    return [
        BBox(south=south, west=west, north=north, east=east)
        for south, north in ((bbox.south, mid_lat), (mid_lat, bbox.north))
        for west, east in ((bbox.west, mid_lon), (mid_lon, bbox.east))
    ]


def chunk_bboxes(bbox: BBox, max_chunk_area_km2: float) -> list[BBox]:
    """Split ``bbox`` into a grid of cells no larger than ``max_chunk_area_km2``.

    Overpass cost grows with the queried extent and a single large request can
    time out on a busy public server. Every cell is at most
    ``sqrt(max_chunk_area_km2)`` kilometres on a side, so a big selection becomes
    several small requests that are cached individually and merged afterwards.
    """

    limit = float(max_chunk_area_km2)
    if not math.isfinite(limit) or limit <= 0.0:
        return [bbox]

    side_m = math.sqrt(limit) * 1000.0
    width_m, height_m = _extent_m(bbox)
    # A hair over the limit is expected from the metre/degree conversion; do not
    # let that turn a 2x2 split into a wasteful 3x3 one.
    n_lat = max(1, int(math.ceil(height_m / side_m - 1e-3)))
    n_lon = max(1, int(math.ceil(width_m / side_m - 1e-3)))
    if n_lat == 1 and n_lon == 1:
        return [bbox]

    lat_step = (bbox.north - bbox.south) / n_lat
    lon_step = (bbox.east - bbox.west) / n_lon
    if lat_step <= 0.0 or lon_step <= 0.0:
        return [bbox]

    cells: list[BBox] = []
    for i in range(n_lat):
        south = bbox.south + lat_step * i
        north = bbox.north if i == n_lat - 1 else bbox.south + lat_step * (i + 1)
        for j in range(n_lon):
            west = bbox.west + lon_step * j
            east = bbox.east if j == n_lon - 1 else bbox.west + lon_step * (j + 1)
            cells.append(BBox(south=south, west=west, north=north, east=east))
    return cells


def _merge_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge Overpass payloads, dropping elements returned by several chunks."""

    seen: set[tuple[str, Any]] = set()
    elements: list[dict[str, Any]] = []
    for payload in payloads:
        for element in payload.get("elements") or []:
            key = (str(element.get("type")), element.get("id"))
            if key in seen:
                continue
            seen.add(key)
            elements.append(element)
    first = payloads[0] if payloads else {}
    return {
        "version": first.get("version", 0.6),
        "generator": first.get("generator", "Overpass API"),
        "elements": elements,
    }


@dataclass
class OverpassResult:
    raw: dict[str, Any]
    meta: dict[str, Any]
    from_cache: bool


@dataclass
class _CellFetch:
    """One chunk-sized fetch, whether it came from cache, HTTP, or a split."""

    raw: dict[str, Any]
    endpoints: list[str]
    from_cache: bool
    timestamp: str


class OverpassClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(settings.overpass_timeout_s),
            follow_redirects=True,
        )
        self._sleep = sleep
        self._throttle_lock = threading.Lock()
        self._last_request_at = 0.0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OverpassClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @staticmethod
    def build_query(bbox: BBox) -> str:
        box = bbox.as_overpass()
        return (
            "[out:json][timeout:180];\n"
            "(\n"
            f'  way["building"]({box});\n'
            f'  relation["building"]({box});\n'
            ");\n"
            "out body;\n"
            ">;\n"
            "out skel qt;\n"
        )

    def _cache_paths(self, bbox: BBox) -> tuple[Path, Path]:
        digest = sha256_text(f"{QUERY_VERSION}|{bbox.as_overpass()}")
        base = self.settings.cache_dir / "osm"
        return base / f"{digest}.json", base / f"{digest}.meta.json"

    def _load_cache(self, bbox: BBox) -> OverpassResult | None:
        data_path, meta_path = self._cache_paths(bbox)
        if not data_path.exists() or not meta_path.exists():
            return None
        if is_older_than(data_path, self.settings.cache_ttl_days):
            return None
        try:
            raw = read_json(data_path)
            meta = read_json(meta_path)
        except Exception:
            return None
        if not isinstance(raw, dict) or not isinstance(meta, dict):
            return None
        return OverpassResult(raw=raw, meta=meta, from_cache=True)

    def _save_cache(self, bbox: BBox, raw: dict[str, Any], meta: dict[str, Any]) -> None:
        data_path, meta_path = self._cache_paths(bbox)
        write_json_atomic(data_path, raw)
        write_json_atomic(meta_path, meta)

    def _throttle(self) -> None:
        interval = float(self.settings.overpass_min_interval_s)
        if interval <= 0:
            return
        with self._throttle_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < interval:
                self._sleep(interval - elapsed)
            self._last_request_at = time.monotonic()

    def _request_once(self, endpoint: str, query: str) -> dict[str, Any]:
        self._throttle()
        headers = {"User-Agent": self.settings.overpass_user_agent}
        response = self._client.post(
            endpoint,
            data={"data": query},
            headers=headers,
        )
        if response.status_code != 200:
            raise _RetryableStatus(
                f"Overpass 返回 HTTP {response.status_code}",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise OverpassError("Overpass 返回了非 JSON 内容", str(exc)) from exc

        if not isinstance(payload, dict):
            raise OverpassError("Overpass 返回格式无效")

        remark = str(payload.get("remark", ""))
        if remark and ("error" in remark.lower() or "timed out" in remark.lower()):
            raise OverpassError("Overpass 查询失败", remark)

        if "elements" not in payload:
            raise OverpassError("Overpass 返回缺少 elements 字段")
        return payload

    def _request(self, bbox: BBox) -> tuple[dict[str, Any], str]:
        """Fetch one bbox, trying every endpoint and retry; return (raw, endpoint)."""

        query = self.build_query(bbox)
        endpoints = self.settings.endpoints
        if not endpoints:
            raise OverpassError("未配置 Overpass 端点")

        last_error: Exception | None = None
        for endpoint in endpoints:
            for attempt in range(max(1, self.settings.overpass_max_retries)):
                try:
                    raw = self._request_once(endpoint, query)
                except _RetryableStatus as exc:
                    last_error = exc
                    logger.warning(
                        "Overpass retryable failure endpoint=%s status=%s attempt=%s",
                        endpoint,
                        exc.status_code,
                        attempt + 1,
                    )
                    delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
                    self._sleep(delay)
                    continue
                except (httpx.HTTPError, OverpassError) as exc:
                    last_error = exc
                    logger.warning(
                        "Overpass failure endpoint=%s attempt=%s error=%s",
                        endpoint,
                        attempt + 1,
                        exc,
                    )
                    delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
                    self._sleep(delay)
                    continue
                return raw, endpoint

        detail = str(last_error) if last_error else None
        raise OverpassError(
            "无法从 Overpass 获取数据，请稍后重试或更换端点",
            detail,
        ) from last_error

    def _meta(
        self,
        bbox: BBox,
        endpoint: str,
        raw: dict[str, Any],
        *,
        chunks: int = 1,
    ) -> dict[str, Any]:
        meta = {
            "query_version": QUERY_VERSION,
            "query_hash": sha256_text(f"{QUERY_VERSION}|{bbox.as_overpass()}"),
            "bbox": bbox.model_dump(),
            "endpoint": endpoint,
            "fetched_at": utc_now_iso(),
            "osm_base_timestamp": str((raw.get("osm3s") or {}).get("timestamp_osm_base", "")),
            "chunks": chunks,
        }
        return meta

    def _split_cells(self, bbox: BBox, depth: int) -> list[BBox]:
        """The four child cells to try after ``bbox`` exhausted its retries.

        The rectangle is cut at its mid-latitude and mid-longitude, so each child
        has a quarter of the parent's area regardless of aspect ratio (an
        area-based ``chunk_bboxes`` call rounds each axis up and can produce
        eight sub-minimum children for an elongated cell). ``depth`` counts
        splits already applied to the enclosing top-level chunk; the minimum
        area keeps the recursion from producing cells too small for Overpass to
        be useful.
        """

        if depth >= max(0, int(self.settings.overpass_split_max_depth)):
            return []
        parent_area = _planar_area_km2(bbox)
        if parent_area <= 0.0:
            return []
        # The planner allows the nominal side length a small slack, so compare
        # with the same tolerance: a nominal 1 km² cell must still be allowed to
        # produce its nominal 0.25 km² children.
        min_area = float(self.settings.overpass_split_min_area_km2)
        floor_area = min_area * (1.0 - 1e-3)
        if parent_area / 4.0 < floor_area:
            return []
        children = quarter_bboxes(bbox)
        if any(_planar_area_km2(child) < floor_area for child in children):
            return []
        return children

    @staticmethod
    def _cell_time(
        bbox: BBox,
        depth: int,
        seconds: float,
        *,
        from_cache: bool,
        split: bool = False,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "bbox": bbox.model_dump(),
            "depth": depth,
            "seconds": round(seconds, 3),
            "from_cache": from_cache,
        }
        if split:
            record["split"] = True
        return record

    def _fetch_cell(
        self,
        bbox: BBox,
        *,
        depth: int,
        refresh: bool,
        splits: list[dict[str, Any]],
        cell_times: list[dict[str, Any]],
    ) -> _CellFetch:
        """Fetch one cell, splitting it into sub-cells if it keeps failing.

        A failed cell is only a whole-job failure once its retries, the depth
        limit, and the minimum cell area all say it cannot be subdivided.
        """

        if not refresh:
            cached = self._load_cache(bbox)
            if cached is not None:
                cell_times.append(self._cell_time(bbox, depth, 0.0, from_cache=True))
                return _CellFetch(
                    raw=cached.raw,
                    endpoints=[str(cached.meta.get("endpoint", ""))],
                    from_cache=True,
                    timestamp=str(cached.meta.get("osm_base_timestamp", "")),
                )

        started = time.monotonic()
        try:
            raw, endpoint = self._request(bbox)
        except OverpassError as exc:
            elapsed = time.monotonic() - started
            children = self._split_cells(bbox, depth)
            if not children:
                cell_times.append(self._cell_time(bbox, depth, elapsed, from_cache=False))
                raise

            split_start = len(splits)
            time_start = len(cell_times)
            splits.append(
                {
                    "bbox": bbox.model_dump(),
                    "depth": depth,
                    "children": len(children),
                    "error": str(exc),
                    "detail": str(exc.detail) if exc.detail else "",
                }
            )
            cell_times.append(
                self._cell_time(bbox, depth, elapsed, from_cache=False, split=True)
            )
            logger.warning(
                "Overpass cell failed at depth %s; retrying as %s sub-cells: %s",
                depth,
                len(children),
                exc,
            )

            payloads: list[dict[str, Any]] = []
            endpoints: list[str] = []
            timestamps: list[str] = []
            all_cached = True
            for child in children:
                fetched = self._fetch_cell(
                    child,
                    depth=depth + 1,
                    refresh=refresh,
                    splits=splits,
                    cell_times=cell_times,
                )
                payloads.append(fetched.raw)
                endpoints.extend(fetched.endpoints)
                timestamps.append(fetched.timestamp)
                all_cached = all_cached and fetched.from_cache

            merged = _merge_payloads(payloads)
            stamped = [value for value in timestamps if value]
            meta = self._meta(bbox, endpoints[0] if endpoints else "", merged)
            meta["osm_base_timestamp"] = min(stamped) if stamped else ""
            meta["overpass_splits"] = splits[split_start:]
            meta["cell_times"] = cell_times[time_start:]
            self._save_cache(bbox, merged, meta)
            return _CellFetch(
                raw=merged,
                endpoints=endpoints,
                from_cache=all_cached,
                timestamp=meta["osm_base_timestamp"],
            )

        elapsed = time.monotonic() - started
        cell_times.append(self._cell_time(bbox, depth, elapsed, from_cache=False))
        meta = self._meta(bbox, endpoint, raw)
        self._save_cache(bbox, raw, meta)
        return _CellFetch(
            raw=raw,
            endpoints=[endpoint],
            from_cache=False,
            timestamp=meta["osm_base_timestamp"],
        )

    def fetch(
        self,
        bbox: BBox,
        *,
        refresh: bool = False,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> OverpassResult:
        if not refresh:
            cached = self._load_cache(bbox)
            if cached is not None:
                logger.info("Overpass cache hit for %s", bbox.as_overpass())
                # The split/timing block in the cache describes the fetch that
                # filled it; a top-level hit touches no cells this run.
                cached.meta["overpass_splits"] = []
                cached.meta["cell_times"] = []
                return cached

        cells = chunk_bboxes(bbox, self.settings.overpass_chunk_area_km2)
        if len(cells) > 1:
            logger.info(
                "Overpass fetch split into %s chunks for %s", len(cells), bbox.as_overpass()
            )
        splits: list[dict[str, Any]] = []
        cell_times: list[dict[str, Any]] = []
        payloads: list[dict[str, Any]] = []
        timestamps: list[str] = []
        endpoints: list[str] = []
        all_cached = True
        for done, cell in enumerate(cells, start=1):
            fetched = self._fetch_cell(
                cell,
                depth=0,
                refresh=refresh,
                splits=splits,
                cell_times=cell_times,
            )
            payloads.append(fetched.raw)
            timestamps.append(fetched.timestamp)
            endpoints.extend(fetched.endpoints)
            all_cached = all_cached and fetched.from_cache
            if progress_cb is not None:
                progress_cb(done, len(cells))

        merged = _merge_payloads(payloads)
        stamped = [value for value in timestamps if value]
        if len(cells) == 1:
            raw = payloads[0]
            meta = self._meta(bbox, endpoints[0] if endpoints else "", raw)
        else:
            raw = merged
            meta = self._meta(
                bbox,
                endpoints[0] if endpoints else "",
                merged,
                chunks=len(cells),
            )
            meta["chunk_endpoints"] = sorted(set(endpoints))
        # Mixed cell ages: report the oldest base timestamp so the provenance
        # never claims to be fresher than the stalest cell.
        meta["osm_base_timestamp"] = min(stamped) if stamped else ""
        meta["overpass_splits"] = splits
        meta["cell_times"] = cell_times
        self._save_cache(bbox, raw, meta)
        return OverpassResult(raw=raw, meta=meta, from_cache=all_cached)


class _RetryableStatus(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
