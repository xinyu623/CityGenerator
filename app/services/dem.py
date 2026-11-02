"""Elevation tile client for the AWS Terrain Tiles (Terrarium) dataset.

Tiles are 256x256 8-bit RGB PNGs where ``metres = R*256 + G + B/256 - 32768``.
Only the relative relief matters to the pipeline, so the vertical datum of the
source is recorded in metadata but never used as an absolute reference.
"""

from __future__ import annotations

import io
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

from app.config import Settings
from app.services.errors import JobCancelled, TerrainError
from app.services.io_utils import is_older_than, write_bytes_atomic
from app.services.terrain import ElevationData, ProgressCallback

SOURCE_ID = "aws-terrarium"
SOURCE_VERSION = "1"
TILE_PX = 256
NODATA_LIMIT_M = -1000.0
WEB_MERCATOR_LAT = 85.05112878
EARTH_CIRCUMFERENCE_M = 40075016.68557849
MAX_DEM_RETRIES = 3
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_BACKOFF_SECONDS = (1.0, 3.0, 8.0)


def pixel_size_m(latitude: float, zoom: int) -> float:
    return EARTH_CIRCUMFERENCE_M * math.cos(math.radians(latitude)) / (TILE_PX * 2**zoom)


def choose_zoom(
    latitude: float,
    resolution_m: float,
    *,
    zoom_min: int,
    zoom_max: int,
) -> tuple[int, bool]:
    """Return the coarsest zoom whose pixel is no larger than the resolution."""

    for zoom in range(zoom_min, zoom_max + 1):
        if pixel_size_m(latitude, zoom) <= resolution_m:
            return zoom, False
    return zoom_max, True


def lonlat_to_tile(
    longitude: np.ndarray | float,
    latitude: np.ndarray | float,
    zoom: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Fractional slippy-map tile coordinates (origin at the north-west corner)."""

    scale = float(2**zoom)
    lon = np.asarray(longitude, dtype=float)
    lat = np.clip(np.asarray(latitude, dtype=float), -WEB_MERCATOR_LAT, WEB_MERCATOR_LAT)
    x = (lon + 180.0) / 360.0 * scale
    y = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2.0 * scale
    return np.clip(x, 0.0, scale), np.clip(y, 0.0, scale)


def decode_terrarium(image: Image.Image) -> np.ndarray:
    """Decode a terrarium tile into metres (NaN where the tile has no data)."""

    rgb = np.asarray(image.convert("RGB"), dtype=np.float64)
    values = rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0
    return np.where(values <= NODATA_LIMIT_M, np.nan, values)


@dataclass
class _TilePlan:
    zoom: int
    x0: int
    y0: int
    x1: int
    y1: int
    limited_by_max_zoom: bool
    zoom_reduced: bool

    @property
    def count(self) -> int:
        return (self.x1 - self.x0 + 1) * (self.y1 - self.y0 + 1)


class TerrariumSource:
    """Download, cache, mosaic, and sample terrarium elevation tiles."""

    name = SOURCE_ID

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
            timeout=httpx.Timeout(settings.dem_timeout_s),
            follow_redirects=True,
        )
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TerrariumSource:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # -- cache ---------------------------------------------------------
    def _tile_path(self, zoom: int, x: int, y: int) -> Path:
        return (
            self.settings.cache_dir
            / "dem"
            / f"{SOURCE_ID}-{SOURCE_VERSION}"
            / str(zoom)
            / str(x)
            / f"{y}.png"
        )

    def _load_cached(self, zoom: int, x: int, y: int) -> bytes | None:
        path = self._tile_path(zoom, x, y)
        if not path.is_file():
            return None
        if is_older_than(path, self.settings.cache_ttl_days):
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _store_cached(self, zoom: int, x: int, y: int, data: bytes) -> None:
        try:
            write_bytes_atomic(self._tile_path(zoom, x, y), data)
        except OSError:
            # A read-only cache directory must not fail a generation job.
            pass

    # -- tiles ---------------------------------------------------------
    def _tile_url(self, zoom: int, x: int, y: int) -> str:
        return self.settings.dem_url_template.format(z=zoom, x=x, y=y)

    def _download(self, zoom: int, x: int, y: int) -> bytes:
        url = self._tile_url(zoom, x, y)
        headers = {"User-Agent": self.settings.dem_user_agent}
        last_error: Exception | None = None
        for attempt in range(max(1, int(self.settings.dem_max_retries))):
            try:
                response = self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code == 200 and response.content:
                    return response.content
                last_error = TerrainError(
                    "地形高程瓦片请求失败",
                    f"HTTP {response.status_code} · {url}",
                )
                if response.status_code not in _RETRYABLE_STATUS:
                    raise last_error
            if attempt + 1 < max(1, int(self.settings.dem_max_retries)):
                delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
                self._sleep(delay)

        detail = str(last_error) if last_error else None
        raise TerrainError(
            "无法获取地形高程数据，请检查网络或稍后重试",
            detail,
        ) from last_error

    def _tile_bytes(self, zoom: int, x: int, y: int) -> tuple[bytes, bool]:
        cached = self._load_cached(zoom, x, y)
        if cached is not None:
            return cached, True
        data = self._download(zoom, x, y)
        self._store_cached(zoom, x, y, data)
        return data, False

    # -- planning ------------------------------------------------------
    def _plan_tiles(
        self, lons: np.ndarray, lats: np.ndarray, resolution_m: float
    ) -> _TilePlan:
        centre_lat = float(np.clip(np.mean(lats), -80.0, 80.0))
        zoom, limited = choose_zoom(
            centre_lat,
            float(resolution_m),
            zoom_min=self.settings.dem_zoom_min,
            zoom_max=self.settings.dem_zoom_max,
        )
        max_tiles = max(1, int(self.settings.dem_max_tiles))
        reduced = False

        while True:
            fx0, fy0 = lonlat_to_tile(float(lons.min()), float(lats.max()), zoom)
            fx1, fy1 = lonlat_to_tile(float(lons.max()), float(lats.min()), zoom)
            limit = 2**zoom - 1
            x0 = min(max(int(math.floor(float(fx0))), 0), limit)
            y0 = min(max(int(math.floor(float(fy0))), 0), limit)
            x1 = min(max(int(math.floor(float(fx1))), 0), limit)
            y1 = min(max(int(math.floor(float(fy1))), 0), limit)
            plan = _TilePlan(zoom, x0, y0, x1, y1, limited, reduced)
            if plan.count <= max_tiles or zoom <= self.settings.dem_zoom_min:
                return plan
            zoom -= 1
            reduced = True

    # -- sampling ------------------------------------------------------
    def fetch(
        self,
        lons: np.ndarray,
        lats: np.ndarray,
        *,
        resolution_m: float,
        cancel_event: threading.Event | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> ElevationData:
        longitude = np.asarray(lons, dtype=float)
        latitude = np.asarray(lats, dtype=float)
        if longitude.size == 0:
            raise TerrainError("地形采样点为空")

        plan = self._plan_tiles(longitude, latitude, float(resolution_m))
        width = (plan.x1 - plan.x0 + 1) * TILE_PX
        height = (plan.y1 - plan.y0 + 1) * TILE_PX
        mosaic = np.full((height, width), np.nan, dtype=float)

        cached_tiles = 0
        done = 0
        total = plan.count
        for ty in range(plan.y0, plan.y1 + 1):
            for tx in range(plan.x0, plan.x1 + 1):
                if cancel_event is not None and cancel_event.is_set():
                    raise JobCancelled("任务已取消")
                data, from_cache = self._tile_bytes(plan.zoom, tx, ty)
                cached_tiles += int(from_cache)
                tile = decode_terrarium(Image.open(io.BytesIO(data)))
                row = (ty - plan.y0) * TILE_PX
                col = (tx - plan.x0) * TILE_PX
                mosaic[row : row + TILE_PX, col : col + TILE_PX] = tile
                done += 1
                if progress_cb is not None:
                    progress_cb(done, total)

        fx, fy = lonlat_to_tile(longitude, latitude, plan.zoom)
        local_x = fx * TILE_PX - plan.x0 * TILE_PX
        local_y = fy * TILE_PX - plan.y0 * TILE_PX
        values = _bilinear(mosaic, local_x, local_y)
        missing = int(np.isnan(values).sum())

        return ElevationData(
            values=values,
            meta={
                "enabled": True,
                "source": SOURCE_ID,
                "source_version": SOURCE_VERSION,
                "zoom": plan.zoom,
                "pixel_m": round(pixel_size_m(float(np.mean(latitude)), plan.zoom), 2),
                "tiles": plan.count,
                "cached_tiles": cached_tiles,
                "nodata_pixels": missing,
                "limited_by_max_zoom": plan.limited_by_max_zoom,
                "zoom_reduced": plan.zoom_reduced,
            },
        )


def _bilinear(mosaic: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Sample the mosaic at fractional pixel coordinates with edge clamping."""

    height, width = mosaic.shape
    x = np.clip(np.asarray(x, dtype=float), 0.0, width - 1.0)
    y = np.clip(np.asarray(y, dtype=float), 0.0, height - 1.0)
    x0 = np.clip(np.floor(x).astype(np.int64), 0, max(width - 2, 0))
    y0 = np.clip(np.floor(y).astype(np.int64), 0, max(height - 2, 0))
    x1 = np.clip(x0 + 1, 0, width - 1)
    y1 = np.clip(y0 + 1, 0, height - 1)
    fx = np.clip(x - x0, 0.0, 1.0)
    fy = np.clip(y - y0, 0.0, 1.0)

    z00 = mosaic[y0, x0]
    z10 = mosaic[y0, x1]
    z01 = mosaic[y1, x0]
    z11 = mosaic[y1, x1]
    lower = z00 * (1.0 - fx) + z10 * fx
    upper = z01 * (1.0 - fx) + z11 * fx
    return lower * (1.0 - fy) + upper * fy


def tile_cache_dir(settings: Settings) -> Path:
    return settings.cache_dir / "dem"


def purge_tile_cache(settings: Settings, days: float) -> int:
    """Delete stale DEM tiles; return the number of removed files."""

    root = tile_cache_dir(settings)
    if not root.exists():
        return 0
    removed = 0
    for path in sorted(root.rglob("*.png")):
        if not is_older_than(path, days):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    for directory in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass
    return removed
