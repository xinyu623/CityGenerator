from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.models.schemas import BBox
from app.services.overpass import OverpassClient

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "overpass_tiny.json").read_text(encoding="utf-8")
)
BBOX = BBox(south=28.20, west=112.90, north=28.21, east=112.91)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "data_dir": tmp_path,
        "overpass_endpoints": "https://overpass.example/api/interpreter",
        "overpass_min_interval_s": 0.0,
        "overpass_max_retries": 2,
        "overpass_timeout_s": 5,
    }
    values.update(overrides)
    return Settings(**values)


def test_query_contains_building_and_bbox():
    client = OverpassClient(make_settings(Path(".")), client=httpx.Client())
    query = client.build_query(BBOX)
    assert '["building"]' in query
    assert "28.200000,112.900000,28.210000,112.910000" in query
    assert "out body" in query


def test_fetch_and_cache(tmp_path):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=FIXTURE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(make_settings(tmp_path), client=client)

    first = overpass.fetch(BBOX)
    assert first.from_cache is False
    assert first.raw["elements"]
    assert calls["count"] == 1

    second = overpass.fetch(BBOX)
    assert second.from_cache is True
    assert calls["count"] == 1


def test_refresh_bypasses_cache(tmp_path):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=FIXTURE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(make_settings(tmp_path), client=client)
    overpass.fetch(BBOX)
    overpass.fetch(BBOX, refresh=True)
    assert calls["count"] == 2


def test_retries_on_rate_limit(tmp_path):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, text="too many requests")
        return httpx.Response(200, json=FIXTURE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(make_settings(tmp_path), client=client, sleep=lambda _: None)
    result = overpass.fetch(BBOX)
    assert result.from_cache is False
    assert calls["count"] == 2


def test_raises_overpass_error_after_failures(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, text="gateway timeout")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(make_settings(tmp_path), client=client, sleep=lambda _: None)
    with pytest.raises(Exception) as excinfo:
        overpass.fetch(BBOX)
    assert (
        "OVERPASS" in str(excinfo.value.code) or "OVERPASS" in type(excinfo.value).__name__.upper()
    )


def test_rejects_overpass_remark_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"elements": [], "remark": "runtime error: query timed out"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(make_settings(tmp_path), client=client, sleep=lambda _: None)
    with pytest.raises(Exception) as excinfo:
        overpass.fetch(BBOX)
    assert getattr(excinfo.value, "code", "") == "OVERPASS_ERROR"
