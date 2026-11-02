from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest
from pyproj import Geod

from app.config import Settings
from app.models.schemas import BBox
from app.services.overpass import OverpassClient, chunk_bboxes

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "overpass_tiny.json").read_text(encoding="utf-8")
)
BBOX = BBox(south=28.20, west=112.90, north=28.21, east=112.91)
BIG_BBOX = BBox(south=29.553, west=106.546686, north=29.589069, east=106.588)
_GEOD = Geod(ellps="WGS84")
_QUERY_BBOX = re.compile(r"\(([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+)\)")


def area_km2(bbox: BBox) -> float:
    lons = [bbox.west, bbox.east, bbox.east, bbox.west]
    lats = [bbox.south, bbox.south, bbox.north, bbox.north]
    area, _ = _GEOD.polygon_area_perimeter(lons, lats)
    return abs(area) / 1_000_000.0


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


def test_chunk_bboxes_covers_the_parent_within_the_area_limit():
    cells = chunk_bboxes(BIG_BBOX, 4.0)

    assert len(cells) == 4
    assert min(cell.south for cell in cells) == pytest.approx(BIG_BBOX.south)
    assert max(cell.north for cell in cells) == pytest.approx(BIG_BBOX.north)
    assert min(cell.west for cell in cells) == pytest.approx(BIG_BBOX.west)
    assert max(cell.east for cell in cells) == pytest.approx(BIG_BBOX.east)
    for cell in cells:
        assert area_km2(cell) <= 4.0 * 1.01
    assert sum(area_km2(cell) for cell in cells) == pytest.approx(area_km2(BIG_BBOX), rel=1e-6)


def test_small_bbox_is_a_single_chunk():
    assert chunk_bboxes(BBOX, 4.0) == [BBOX]


def test_large_fetch_is_split_and_merged(tmp_path):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = unquote_plus(request.content.decode())
        requests.append(query)
        match = _QUERY_BBOX.search(query)
        assert match
        south, west, north, east = (float(value) for value in match.groups())
        payload = {
            "version": 0.6,
            "generator": "Overpass API",
            "osm3s": {"timestamp_osm_base": "2026-11-01T00:00:00Z"},
            "elements": [
                # Returned by every chunk: must be de-duplicated.
                {"type": "way", "id": 1, "tags": {"building": "yes"}},
                # One unique node per chunk.
                {"type": "node", "id": int(round((south + west) * 1e6))},
            ],
        }
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(make_settings(tmp_path), client=client, sleep=lambda _: None)
    result = overpass.fetch(BIG_BBOX)

    assert len(requests) == 4
    ids = [element["id"] for element in result.raw["elements"]]
    assert len(ids) == len(set(ids)) == 5  # shared way + four unique nodes
    assert result.meta["chunks"] == 4
    assert result.meta["osm_base_timestamp"] == "2026-11-01T00:00:00Z"
    assert result.from_cache is False

    # The merged answer is cached under the parent bbox.
    before = len(requests)
    again = overpass.fetch(BIG_BBOX)
    assert again.from_cache is True
    assert len(requests) == before


def test_chunk_results_are_cached_individually(tmp_path):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content.decode())
        return httpx.Response(
            200,
            json={
                "version": 0.6,
                "elements": [{"type": "way", "id": 1, "tags": {"building": "yes"}}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(make_settings(tmp_path), client=client, sleep=lambda _: None)
    overpass.fetch(BIG_BBOX)
    assert len(requests) == 4

    # Drop only the merged parent entry: every chunk is still on disk, so the
    # next fetch must not hit the network at all.
    data_path, meta_path = overpass._cache_paths(BIG_BBOX)
    data_path.unlink()
    meta_path.unlink()
    result = overpass.fetch(BIG_BBOX)
    assert len(requests) == 4
    assert result.from_cache is True
    assert result.meta["chunks"] == 4


def test_chunked_fetch_reports_progress(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"version": 0.6, "elements": [{"type": "way", "id": 1}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(make_settings(tmp_path), client=client, sleep=lambda _: None)
    seen: list[tuple[int, int]] = []
    overpass.fetch(BIG_BBOX, progress_cb=lambda done, total: seen.append((done, total)))

    assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_one_failed_chunk_fails_the_whole_fetch(tmp_path):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] > 1:
            return httpx.Response(504, text="gateway timeout")
        return httpx.Response(
            200,
            json={"version": 0.6, "elements": [{"type": "way", "id": 1}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(
        make_settings(tmp_path, overpass_max_retries=1),
        client=client,
        sleep=lambda _: None,
    )
    with pytest.raises(Exception) as excinfo:
        overpass.fetch(BIG_BBOX)
    assert getattr(excinfo.value, "code", "") == "OVERPASS_ERROR"


# --- adaptive split on timeout (P2-1) -------------------------------------


def request_bbox(request: httpx.Request) -> BBox:
    """The bbox a MockTransport request actually queried (6-decimal form)."""

    query = unquote_plus(request.content.decode())
    match = _QUERY_BBOX.search(query)
    assert match
    south, west, north, east = (float(value) for value in match.groups())
    return BBox(south=south, west=west, north=north, east=east)


def cell_payload(*ids: int) -> dict:
    return {
        "version": 0.6,
        "osm3s": {"timestamp_osm_base": "2026-11-02T00:00:00Z"},
        "elements": [{"type": "way", "id": value} for value in ids],
    }


def test_timed_out_chunk_splits_into_four_and_recovers(tmp_path):
    cells = chunk_bboxes(BIG_BBOX, 4.0)
    failing = cells[1]
    children = chunk_bboxes(failing, area_km2(failing) / 4.0)
    top_ids = {cell.as_overpass(): 10 + index for index, cell in enumerate(cells)}
    child_ids = {child.as_overpass(): 100 + index for index, child in enumerate(children)}
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request_bbox(request).as_overpass()
        requests.append(key)
        if key == failing.as_overpass():
            return httpx.Response(504, text="gateway timeout")
        # A shared way proves the merged set is de-duplicated across cells.
        element_id = top_ids[key] if key in top_ids else child_ids[key]
        return httpx.Response(200, json=cell_payload(1, element_id))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(make_settings(tmp_path), client=client, sleep=lambda _: None)
    result = overpass.fetch(BIG_BBOX)

    assert result.from_cache is False
    assert result.meta["chunks"] == 4
    # The failed chunk exhausted its two retries, then its four children answered.
    assert requests.count(failing.as_overpass()) == 2
    assert len(requests) == 3 + 2 + 4
    # Three surviving chunks plus four sub-cells, with the shared way once.
    assert {element["id"] for element in result.raw["elements"]} == {
        1,
        10,
        12,
        13,
        100,
        101,
        102,
        103,
    }
    assert result.meta["osm_base_timestamp"] == "2026-11-02T00:00:00Z"

    splits = result.meta["overpass_splits"]
    assert len(splits) == 1
    assert splits[0]["bbox"] == failing.model_dump()
    assert splits[0]["depth"] == 0
    assert splits[0]["children"] == 4
    assert splits[0]["error"]
    assert splits[0]["detail"] == "Overpass 返回 HTTP 504"
    times = result.meta["cell_times"]
    assert len(times) == 4 + 4
    assert all(entry["seconds"] >= 0.0 for entry in times)
    split_times = [entry for entry in times if entry.get("split")]
    assert len(split_times) == 1
    assert split_times[0]["bbox"] == failing.model_dump()
    assert split_times[0]["depth"] == 0
    assert split_times[0]["from_cache"] is False

    # Every sub-cell is cached under its own bbox, as the merged parent is.
    for child in children:
        data_path, meta_path = overpass._cache_paths(child)
        assert data_path.exists() and meta_path.exists()

    # Dropping the merged 4 x 4 km entry must still be served from the cached
    # cells, including the split chunk's own merged entry.
    data_path, meta_path = overpass._cache_paths(BIG_BBOX)
    data_path.unlink()
    meta_path.unlink()
    again = overpass.fetch(BIG_BBOX)
    assert len(requests) == 3 + 2 + 4
    assert again.from_cache is True


def test_split_recurses_two_levels_before_giving_up(tmp_path):
    parent = BIG_BBOX
    level_one = chunk_bboxes(parent, area_km2(parent) / 4.0)
    level_two = [
        child for cell in level_one for child in chunk_bboxes(cell, area_km2(cell) / 4.0)
    ]
    slow = {parent.as_overpass(), *(cell.as_overpass() for cell in level_one)}
    leaf_ids = {cell.as_overpass(): 200 + index for index, cell in enumerate(level_two)}
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request_bbox(request).as_overpass()
        requests.append(key)
        if key in slow:
            return httpx.Response(504, text="gateway timeout")
        return httpx.Response(200, json=cell_payload(leaf_ids[key]))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(
        make_settings(
            tmp_path, overpass_max_retries=1, overpass_chunk_area_km2=17.0
        ),
        client=client,
        sleep=lambda _: None,
    )
    result = overpass.fetch(BIG_BBOX)

    # One parent attempt, four children, then sixteen grandchildren.
    assert len(requests) == 21
    assert len(result.meta["overpass_splits"]) == 5
    assert [item["depth"] for item in result.meta["overpass_splits"]] == [0, 1, 1, 1, 1]
    assert all(item["children"] == 4 for item in result.meta["overpass_splits"])
    assert len(result.meta["cell_times"]) == 21
    assert {element["id"] for element in result.raw["elements"]} == {
        200 + index for index in range(16)
    }
    assert result.meta["chunks"] == 1


def test_split_stops_at_the_depth_limit(tmp_path):
    # With the whole 16 km² selection as one chunk, two split levels reach
    # ~1 km² cells; depth 2 must not split again.
    areas: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        areas.append(area_km2(request_bbox(request)))
        return httpx.Response(504, text="gateway timeout")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(
        make_settings(
            tmp_path, overpass_max_retries=1, overpass_chunk_area_km2=17.0
        ),
        client=client,
        sleep=lambda _: None,
    )
    with pytest.raises(Exception) as excinfo:
        overpass.fetch(BIG_BBOX)
    assert getattr(excinfo.value, "code", "") == "OVERPASS_ERROR"
    # Parent, one child, one grandchild: the grandchild is depth 2 and stops.
    assert len(areas) == 3
    assert areas[0] == pytest.approx(16.0, rel=0.05)
    assert areas[1] == pytest.approx(4.0, rel=0.05)
    assert areas[2] == pytest.approx(1.0, rel=0.05)


def test_split_stops_at_the_minimum_cell_area(tmp_path):
    # BBOX is ~1.1 km²: one split to ~0.28 km² is allowed, the next would be
    # ~0.07 km² and must not be attempted.
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(504, text="gateway timeout")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    overpass = OverpassClient(
        make_settings(tmp_path, overpass_max_retries=1),
        client=client,
        sleep=lambda _: None,
    )
    with pytest.raises(Exception) as excinfo:
        overpass.fetch(BBOX)
    assert getattr(excinfo.value, "code", "") == "OVERPASS_ERROR"
    # The parent, then exactly one child that cannot be split any further.
    assert calls["count"] == 2
