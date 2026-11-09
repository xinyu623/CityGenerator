from __future__ import annotations

import numpy as np
import pytest

from app.services.terrain import TerrainGrid, grid_spacing


def make_ramp_grid(
    bounds: tuple[float, float, float, float],
    resolution_m: float = 10.0,
    slope: float = 0.02,
) -> TerrainGrid:
    """Deterministic sloping height field with a small hill in the middle."""

    nx, ny, dx, dy, _ = grid_spacing(bounds, resolution_m)
    minx, miny, maxx, maxy = bounds
    xs = minx + dx * np.arange(nx)
    ys = miny + dy * np.arange(ny)
    heights = slope * (xs[None, :] - minx) + 0.5 * slope * (ys[:, None] - miny)
    cx = (minx + maxx) / 2.0
    cy = (miny + maxy) / 2.0
    heights = heights + 4.0 * np.exp(
        -(((xs[None, :] - cx) ** 2 + (ys[:, None] - cy) ** 2) / (2.0 * (0.15 * max(maxx - minx, 1.0)) ** 2))
    )
    datum = float(heights.min())
    return TerrainGrid(
        heights=heights - datum,
        x0=minx,
        y0=miny,
        dx=dx,
        dy=dy,
        datum_offset_m=datum + 40.0,
        elevation_min_m=datum + 40.0,
        elevation_max_m=float(heights.max()) + 40.0,
        meta={"source": "test-ramp", "zoom": 14, "pixel_m": 8.42, "tiles": 1, "cached_tiles": 0},
    )


@pytest.fixture()
def synthetic_terrain(monkeypatch):
    """Replace the DEM fetch with a deterministic local ramp."""

    def fake_build_terrain_grid(
        bounds,
        projector,
        *,
        settings,
        resolution_m=10.0,
        source=None,
        cancel_event=None,
        progress_cb=None,
        core_bounds=None,
        edge_taper_m=0.0,
        water_level_m=None,
        fill_water_islands=False,
        bank_clearance_m=0.0,
    ):
        del projector, settings, source, cancel_event, core_bounds, edge_taper_m, water_level_m
        del fill_water_islands, bank_clearance_m
        grid = make_ramp_grid(bounds, resolution_m)
        if progress_cb is not None:
            progress_cb(1, 1)
        return grid

    monkeypatch.setattr("app.services.pipeline.build_terrain_grid", fake_build_terrain_grid)
    return fake_build_terrain_grid
