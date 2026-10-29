"""Overpass API client with disk cache, throttling, and retries."""

from __future__ import annotations

import logging
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

    def fetch(self, bbox: BBox, *, refresh: bool = False) -> OverpassResult:
        if not refresh:
            cached = self._load_cache(bbox)
            if cached is not None:
                logger.info("Overpass cache hit for %s", bbox.as_overpass())
                return cached

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

                meta = {
                    "query_version": QUERY_VERSION,
                    "query_hash": sha256_text(f"{QUERY_VERSION}|{bbox.as_overpass()}"),
                    "bbox": bbox.model_dump(),
                    "endpoint": endpoint,
                    "fetched_at": utc_now_iso(),
                    "osm_base_timestamp": str(
                        (raw.get("osm3s") or {}).get("timestamp_osm_base", "")
                    ),
                }
                self._save_cache(bbox, raw, meta)
                return OverpassResult(raw=raw, meta=meta, from_cache=False)

        detail = str(last_error) if last_error else None
        raise OverpassError(
            "无法从 Overpass 获取数据，请稍后重试或更换端点",
            detail,
        ) from last_error


class _RetryableStatus(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
