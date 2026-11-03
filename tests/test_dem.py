from __future__ import annotations

import math
from io import BytesIO
from pathlib import Path

import httpx
import numpy as np
import pytest
from PIL import Image

from app.config import Settings
from app.services.dem import (
    TILE_PX,
    TerrariumSource,
    choose_zoom,
    decode_terrarium,
    lonlat_to_tile,
    pixel_size_m,
    purge_tile_cache,
)
from app.services.errors import JobCancelled, TerrainError

LONS = np.array([112.90, 112.91])
LATS = np.array([28.20, 28.21])


def png_bytes(values: float | np.ndarray) -> bytes:
    elevation = np.asarray(values, dtype=np.float64) + 32768.0
    elevation = np.broadcast_to(elevation, (TILE_PX, TILE_PX))
    rgb = np.zeros((TILE_PX, TILE_PX, 3), dtype=np.uint8)
    rgb[..., 0] = np.clip(np.floor(elevation / 256.0), 0, 255).astype(np.uint8)
    rgb[..., 1] = np.clip(np.floor(elevation % 256.0), 0, 255).astype(np.uint8)
    buffer = BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "data_dir": tmp_path,
        "dem_max_retries": 2,
        "dem_timeout_s": 2.0,
        "dem_zoom_min": 10,
        "dem_zoom_max": 14,
        "dem_max_tiles": 8,
    }
    values.update(overrides)
    return Settings(**values)


def make_source(tmp_path: Path, handler, **overrides) -> TerrariumSource:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TerrariumSource(
        make_settings(tmp_path, **overrides),
        client=client,
        sleep=lambda _seconds: None,
    )


def tile_handler(calls: list[str], value_for_x=None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if value_for_x is not None:
            x = int(request.url.path.rsplit("/", 3)[-2])
            value = value_for_x(x)
        else:
            value = 250.0
        return httpx.Response(200, content=png_bytes(value))

    return handler


def test_lonlat_to_tile_corners() -> None:
    x, y = lonlat_to_tile(0.0, 0.0, 1)
    assert float(x) == pytest.approx(1.0)
    assert float(y) == pytest.approx(1.0)

    west, north = lonlat_to_tile(-180.0, 85.05112878, 2)
    assert float(west) == pytest.approx(0.0, abs=1e-9)
    assert float(north) == pytest.approx(0.0, abs=1e-6)


def test_tile_pixel_size_halves_per_zoom() -> None:
    assert pixel_size_m(28.2, 13) / pixel_size_m(28.2, 14) == pytest.approx(2.0)


def test_choose_zoom_picks_the_coarsest_sufficient_zoom() -> None:
    zoom, limited = choose_zoom(28.2, 100.0, zoom_min=10, zoom_max=14)
    assert zoom == 11
    assert limited is False

    assert choose_zoom(28.2, 5000.0, zoom_min=10, zoom_max=14) == (10, False)


def test_choose_zoom_reports_the_max_zoom_limit() -> None:
    zoom, limited = choose_zoom(28.2, 2.0, zoom_min=10, zoom_max=14)
    assert zoom == 14
    assert limited is True


def test_decode_terrarium_roundtrip() -> None:
    encoded = 1234.5 + 32768.0
    rgb = np.zeros((2, 2, 3), dtype=np.uint8)
    rgb[..., 0] = int(encoded // 256)
    rgb[..., 1] = int(encoded % 256)
    rgb[..., 2] = int(round((encoded % 1.0) * 256)) % 256
    values = decode_terrarium(Image.fromarray(rgb, mode="RGB"))
    assert np.allclose(values, 1234.5, atol=1.0 / 256.0)


def test_fetch_samples_a_single_tile(tmp_path) -> None:
    calls: list[str] = []
    source = make_source(tmp_path, tile_handler(calls))
    try:
        data = source.fetch(LONS, LATS, resolution_m=200.0)
    finally:
        source.close()

    assert data.meta["tiles"] == 1
    assert data.meta["zoom"] <= 12
    assert np.allclose(data.values, 250.0, atol=1.0 / 256.0)
    assert data.meta["nodata_pixels"] == 0


def test_fetch_mosaics_neighbouring_tiles(tmp_path) -> None:
    calls: list[str] = []
    values_by_call = iter([100.0, 200.0, 300.0, 400.0])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=png_bytes(next(values_by_call)))

    source = make_source(tmp_path, handler)
    try:
        data = source.fetch(LONS, LATS, resolution_m=10.0)
    finally:
        source.close()

    assert data.meta["tiles"] >= 2
    assert len(calls) == data.meta["tiles"]
    distinct = np.unique(np.round(data.values))
    assert distinct.size >= 2
    assert set(distinct.tolist()) <= {100.0, 200.0, 300.0, 400.0}


def test_fetch_reuses_cached_tiles(tmp_path) -> None:
    calls: list[str] = []
    source = make_source(tmp_path, tile_handler(calls))
    try:
        first = source.fetch(LONS, LATS, resolution_m=10.0)
        requests_after_first = len(calls)
        second = source.fetch(LONS, LATS, resolution_m=10.0)
    finally:
        source.close()

    assert requests_after_first == first.meta["tiles"]
    assert len(calls) == requests_after_first
    assert second.meta["cached_tiles"] == second.meta["tiles"]
    assert np.allclose(first.values, second.values)


def test_fetch_retries_a_transient_failure(tmp_path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=png_bytes(120.0))

    source = make_source(tmp_path, handler, dem_max_tiles=1, dem_zoom_max=10)
    try:
        data = source.fetch(LONS, LATS, resolution_m=5000.0)
    finally:
        source.close()

    assert len(calls) == 2
    assert np.allclose(data.values, 120.0, atol=1.0 / 256.0)


def test_fetch_raises_after_exhausting_retries(tmp_path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500)

    source = make_source(tmp_path, handler, dem_max_retries=2)
    with pytest.raises(TerrainError):
        source.fetch(LONS, LATS, resolution_m=200.0)
    source.close()
    assert len(calls) == 2


def test_tile_budget_reduces_the_zoom(tmp_path) -> None:
    calls: list[str] = []
    source = make_source(tmp_path, tile_handler(calls), dem_max_tiles=1)
    try:
        data = source.fetch(LONS, LATS, resolution_m=10.0)
    finally:
        source.close()

    assert data.meta["tiles"] == 1
    assert data.meta["zoom_reduced"] is True
    assert data.meta["zoom"] < 14


def test_fetch_honours_cancellation(tmp_path) -> None:
    import threading

    calls: list[str] = []
    source = make_source(tmp_path, tile_handler(calls))
    event = threading.Event()
    event.set()
    try:
        with pytest.raises(JobCancelled):
            source.fetch(LONS, LATS, resolution_m=10.0, cancel_event=event)
    finally:
        source.close()
    assert calls == []


def test_purge_tile_cache_removes_stale_tiles(tmp_path) -> None:
    calls: list[str] = []
    source = make_source(tmp_path, tile_handler(calls))
    try:
        data = source.fetch(LONS, LATS, resolution_m=200.0)
    finally:
        source.close()
    settings = make_settings(tmp_path)
    tiles = list((settings.cache_dir / "dem").rglob("*.png"))
    assert len(tiles) == data.meta["tiles"]

    import os
    import time

    stale = time.time() - 40 * 86400
    for path in tiles:
        os.utime(path, (stale, stale))

    assert purge_tile_cache(settings, 7) == len(tiles)
    assert list((settings.cache_dir / "dem").rglob("*.png")) == []


def test_mercator_latitude_is_clamped() -> None:
    x, y = lonlat_to_tile(np.array([0.0]), np.array([89.9]), 3)
    assert math.isfinite(float(y[0]))
    assert 0.0 <= float(y[0]) <= 8.0
    assert math.isfinite(float(x[0]))


def test_source_follows_the_system_proxy_setting(tmp_path, monkeypatch) -> None:
    kwargs_seen: dict[str, object] = {}
    real_client = httpx.Client

    def spy(*args, **kwargs):
        kwargs_seen.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", spy)

    TerrariumSource(make_settings(tmp_path, use_system_proxy=False)).close()
    assert kwargs_seen["trust_env"] is False
