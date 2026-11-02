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
    mid_lat = (bbox.south + bbox.north) / 2.0
    height_m = (bbox.north - bbox.south) * _M_PER_DEG_LAT
    width_m = (
        (bbox.east - bbox.west)
        * _M_PER_DEG_LON_EQUATOR
        * max(math.cos(math.radians(mid_lat)), 0.01)
    )
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
                return cached

        cells = chunk_bboxes(bbox, self.settings.overpass_chunk_area_km2)
        if len(cells) == 1:
            raw, endpoint = self._request(bbox)
            meta = self._meta(bbox, endpoint, raw)
            self._save_cache(bbox, raw, meta)
            if progress_cb is not None:
                progress_cb(1, 1)
            return OverpassResult(raw=raw, meta=meta, from_cache=False)

        logger.info("Overpass fetch split into %s chunks for %s", len(cells), bbox.as_overpass())
        payloads: list[dict[str, Any]] = []
        timestamps: list[str] = []
        endpoints: list[str] = []
        all_cached = True
        for done, cell in enumerate(cells, start=1):
            cached = None if refresh else self._load_cache(cell)
            if cached is not None:
                payloads.append(cached.raw)
                timestamps.append(str(cached.meta.get("osm_base_timestamp", "")))
                endpoints.append(str(cached.meta.get("endpoint", "")))
                if progress_cb is not None:
                    progress_cb(done, len(cells))
                continue
            all_cached = False
            raw, endpoint = self._request(cell)
            meta = self._meta(cell, endpoint, raw)
            self._save_cache(cell, raw, meta)
            payloads.append(raw)
            timestamps.append(meta["osm_base_timestamp"])
            endpoints.append(endpoint)
            if progress_cb is not None:
                progress_cb(done, len(cells))

        merged = _merge_payloads(payloads)
        stamped = [value for value in timestamps if value]
        meta = self._meta(
            bbox,
            endpoints[0] if endpoints else "",
            merged,
            chunks=len(cells),
        )
        # Mixed chunk ages: report the oldest base timestamp so the provenance
        # never claims to be fresher than the stalest cell.
        meta["osm_base_timestamp"] = min(stamped) if stamped else ""
        meta["chunk_endpoints"] = sorted(set(endpoints))
        self._save_cache(bbox, merged, meta)
        return OverpassResult(raw=merged, meta=meta, from_cache=all_cached)


class _RetryableStatus(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
