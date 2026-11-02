from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import Polygon

from app.models.schemas import BBox
from app.services.buildings import extrude_prepared, prepare_buildings
from app.services.city import fuse_city
from app.services.osm_parse import BuildingFeature
from app.services.projection import projector_for_bbox
from app.services.stl_export import (
    export_stl_bytes,
    load_stl_bytes,
    non_manifold_edge_count,
)
from app.services.terrain import TerrainGrid, build_terrain_solid

GOLDEN = Path(__file__).parent / "golden" / "city_300m.json"
BBOX = BBox(south=28.2000, west=112.9000, north=28.2027, east=112.90305)
HEIGHTS_M = (9.0, 12.0, 18.0, 24.0, 30.0)
SIZE_M = 20.0
STEP_M = 50.0
_LAT = 28.2
_M_PER_DEG_LAT = 110_900.0
_M_PER_DEG_LON = 111_320.0 * float(np.cos(np.radians(_LAT)))


def _features() -> list[BuildingFeature]:
    """A deterministic 5 x 5 block of 20 m buildings, heights 9-30 m."""

    dlat = SIZE_M / _M_PER_DEG_LAT
    dlon = SIZE_M / _M_PER_DEG_LON
    step_lat = STEP_M / _M_PER_DEG_LAT
    step_lon = STEP_M / _M_PER_DEG_LON
    features: list[BuildingFeature] = []
    for row in range(5):
        for col in range(5):
            west = BBOX.west + 25.0 / _M_PER_DEG_LON + col * step_lon
            south = BBOX.south + 25.0 / _M_PER_DEG_LAT + row * step_lat
            ring = [
                (west, south),
                (west + dlon, south),
                (west + dlon, south + dlat),
                (west, south + dlat),
            ]
            height = HEIGHTS_M[(row * 5 + col) % len(HEIGHTS_M)]
            features.append(
                BuildingFeature(
                    osm_type="way",
                    osm_id=row * 5 + col + 1,
                    tags={"building": "yes", "height": str(height)},
                    geometry=Polygon(ring),
                )
            )
    return features


def _terrain_grid() -> TerrainGrid:
    nx = ny = 12
    dx = dy = 30.0
    xs = np.arange(nx) * dx
    ys = np.arange(ny) * dy
    heights = (
        2.0
        + 0.025 * xs[None, :]
        + 0.02 * ys[:, None]
        + 1.0 * np.sin(xs[None, :] / 45.0)
    )
    return TerrainGrid(
        heights=heights,
        x0=-30.0,
        y0=-30.0,
        dx=dx,
        dy=dy,
        datum_offset_m=float(heights.min()),
        elevation_min_m=float(heights.min()),
        elevation_max_m=float(heights.max()),
        meta={"source": "golden-ramp"},
    )


def _max_border_overhang_m(grid: TerrainGrid) -> float:
    """Largest vertical step of one terrain border segment (the skirt span)."""

    heights = grid.heights
    steps = np.concatenate(
        [
            np.abs(np.diff(heights[0, :])),
            np.abs(np.diff(heights[-1, :])),
            np.abs(np.diff(heights[:, 0])),
            np.abs(np.diff(heights[:, -1])),
        ]
    )
    return float(steps.max()) if steps.size else 0.0


def build_metrics() -> dict[str, object]:
    projector = projector_for_bbox(BBOX.south, BBOX.west, BBOX.north, BBOX.east)
    prepared = prepare_buildings(_features(), projector, sample_missing_heights=False)
    grid = _terrain_grid()
    build_result = extrude_prepared(prepared, terrain=grid)
    base = build_terrain_solid(grid, thickness_m=1.0)
    city = fuse_city(build_result.meshes, base, base_thickness_m=1.0)
    loaded = load_stl_bytes(export_stl_bytes(city.mesh))
    bounds = [round(float(value), 6) for value in np.asarray(loaded.bounds).ravel()]
    return {
        "buildings": len(build_result.records),
        "triangles": len(loaded.faces),
        "volume_m3": round(float(loaded.volume), 3),
        "area_m2": round(float(loaded.area), 3),
        "bounds": bounds,
        "weld_repairs": city.weld_repairs,
        "watertight": bool(loaded.is_watertight),
        "non_manifold_edges": non_manifold_edge_count(loaded),
        "terrain_triangles": grid.triangle_count,
        "max_border_overhang_m": round(_max_border_overhang_m(grid), 3),
    }


def _assert_matches_golden(actual: dict[str, object], golden: dict[str, object]) -> None:
    for key, expected in golden.items():
        assert key in actual, f"golden metric {key!r} is no longer produced"
        value = actual[key]
        if key == "bounds":
            assert len(value) == len(expected)  # type: ignore[arg-type]
            for found, wanted in zip(value, expected, strict=True):  # type: ignore[arg-type]
                assert found == pytest.approx(wanted, abs=1e-3)
        elif key in ("triangles", "terrain_triangles"):
            assert value == pytest.approx(expected, rel=5e-3)
        elif key in ("volume_m3", "area_m2"):
            assert value == pytest.approx(expected, rel=1e-4)
        else:
            assert value == pytest.approx(expected, abs=1e-3)


def test_golden_300m_city_matches_the_recorded_metrics() -> None:
    metrics = build_metrics()
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(
            json.dumps({"case": "300m block on a synthetic ramp", "metrics": metrics}, indent=2)
            + "\n",
            encoding="utf-8",
        )
    assert GOLDEN.exists(), "golden file missing; run UPDATE_GOLDEN=1 pytest tests/test_golden.py"
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["metrics"]
    _assert_matches_golden(metrics, golden)
