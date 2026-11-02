from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.services.dem import TerrariumSource
from app.services.terrain import build_terrain_grid, build_terrain_solid

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("CITYGEN_RUN_NETWORK") != "1",
        reason="set CITYGEN_RUN_NETWORK=1 to run live DEM tests",
    ),
]


def test_live_dem_returns_relief(tmp_path):
    settings = Settings(data_dir=tmp_path)
    source = TerrariumSource(settings)
    lons = [112.9700, 112.9720, 112.9720, 112.9700]
    lats = [28.1900, 28.1900, 28.1920, 28.1920]

    data = source.fetch(lons, lats, resolution_m=10.0)

    assert data.values.size == 4
    # Changsha is hilly but never near the terrarium nodata floor.
    assert data.values.min() > 0.0
    assert data.meta["source"] == "aws-terrarium"
    assert data.meta["tiles"] >= 1


def test_live_terrain_grid_is_closed(tmp_path):
    from app.models.schemas import BBox
    from app.services.projection import projector_for_bbox

    settings = Settings(data_dir=tmp_path)
    bbox = BBox(south=28.1900, west=112.9700, north=28.1920, east=112.9720)
    projector = projector_for_bbox(*bbox.as_tuple())
    easting, northing = projector.project(bbox.west, bbox.south)
    x0, y0 = projector.to_local(easting, northing)
    easting, northing = projector.project(bbox.east, bbox.north)
    x1, y1 = projector.to_local(easting, northing)
    bounds = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    grid = build_terrain_grid(bounds, projector, settings=settings, resolution_m=10.0)
    solid = build_terrain_solid(grid, thickness_m=1.0)

    assert grid.relief_m > 0.0
    assert solid.is_watertight
    assert solid.is_winding_consistent
    assert solid.volume > 0.0
    assert solid.bounds[0][2] == pytest.approx(-1.0)
