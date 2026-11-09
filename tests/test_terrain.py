from __future__ import annotations

import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon

from app.config import Settings
from app.services.errors import GeometryError, TerrainDataError
from app.services.projection import projector_for_bbox
from app.services.stl_export import (
    export_stl_bytes,
    load_stl_bytes,
    non_manifold_edge_count,
)
from app.services.terrain import (
    ElevationData,
    TerrainGrid,
    _fill_enclosed_land,
    build_terrain_grid,
    build_terrain_solid,
    edge_taper_factor,
    flat_grid,
    grid_spacing,
    smooth_heights,
)

BBOX = (28.20, 112.90, 28.21, 112.91)


def plane_grid(
    nx: int = 5,
    ny: int = 5,
    dx: float = 10.0,
    dy: float = 10.0,
    x0: float = 0.0,
    y0: float = 0.0,
    slope_x: float = 0.02,
    slope_y: float = 0.01,
) -> TerrainGrid:
    xs = x0 + dx * np.arange(nx)
    ys = y0 + dy * np.arange(ny)
    heights = slope_x * xs[None, :] + slope_y * ys[:, None]
    heights = heights - heights.min()
    return TerrainGrid(heights=heights, x0=x0, y0=y0, dx=dx, dy=dy)


def test_surface_z_is_exact_on_a_plane() -> None:
    grid = plane_grid()
    rng = np.random.default_rng(0)
    x = rng.uniform(grid.x0, grid.bounds[2], 50)
    y = rng.uniform(grid.y0, grid.bounds[3], 50)
    base = float(grid.heights.min())
    expected = 0.02 * x + 0.01 * y - base
    assert np.allclose(grid.surface_z(x, y), expected, atol=1e-9)


def test_surface_z_matches_nodes() -> None:
    grid = plane_grid()
    for j in range(grid.ny):
        for i in range(grid.nx):
            x = grid.x0 + grid.dx * i
            y = grid.y0 + grid.dy * j
            assert float(grid.surface_z(x, y)) == pytest.approx(float(grid.heights[j, i]))


def test_surface_normal_follows_the_plane() -> None:
    grid = plane_grid(slope_x=0.02, slope_y=0.01)
    rng = np.random.default_rng(1)
    x = rng.uniform(grid.x0, grid.bounds[2], 20)
    y = rng.uniform(grid.y0, grid.bounds[3], 20)

    normals = grid.surface_normal(x, y)

    expected = np.array([-0.02, -0.01, 1.0])
    expected = expected / np.linalg.norm(expected)
    assert normals.shape == (20, 3)
    assert np.allclose(normals, expected, atol=1e-9)


def test_surface_normal_uses_the_matching_tin_triangle() -> None:
    # A non-planar cell has two different triangle planes; each side of the
    # diagonal must use its own gradient.
    grid = TerrainGrid(
        heights=np.array([[0.0, 1.0], [4.0, 7.0]]),
        x0=0.0,
        y0=0.0,
        dx=10.0,
        dy=10.0,
    )

    lower = grid.surface_normal(np.array([7.5]), np.array([2.5]))[0]
    upper = grid.surface_normal(np.array([2.5]), np.array([7.5]))[0]

    expected_lower = np.array([-0.1, -0.6, 1.0])
    expected_lower = expected_lower / np.linalg.norm(expected_lower)
    expected_upper = np.array([-0.3, -0.4, 1.0])
    expected_upper = expected_upper / np.linalg.norm(expected_upper)
    assert np.allclose(lower, expected_lower)
    assert np.allclose(upper, expected_upper)


def test_surface_normal_is_upward_on_a_flat_grid() -> None:
    grid = plane_grid(slope_x=0.0, slope_y=0.0)

    assert np.allclose(grid.surface_normal(2.0, 3.0), [0.0, 0.0, 1.0])


def test_surface_z_is_continuous_across_the_cell_diagonal() -> None:
    heights = np.array([[0.0, 1.0], [4.0, 7.0]])
    grid = TerrainGrid(heights=heights, x0=0.0, y0=0.0, dx=10.0, dy=10.0)
    # Walk along u == v: both triangles must agree on the shared edge.
    t = np.linspace(0.0, 1.0, 21)
    z = grid.surface_z(t * 10.0, t * 10.0)
    assert np.allclose(z, heights[0, 0] + (heights[1, 1] - heights[0, 0]) * t)


def test_min_over_uses_lowest_covering_node() -> None:
    grid = plane_grid(slope_x=-0.1, slope_y=0.0)
    polygon = Polygon([(10.0, 10.0), (30.0, 10.0), (30.0, 30.0), (10.0, 30.0)])
    # The plane falls towards +X; the covering cells reach the low corner at x=30.
    assert grid.min_over(polygon) == pytest.approx(float(grid.heights[:, 3].min()))


def test_min_over_is_a_lower_bound_for_a_small_footprint() -> None:
    heights = np.array([[0.0, 0.0, 0.0], [0.0, 5.0, 0.0], [0.0, 0.0, 0.0]])
    grid = TerrainGrid(heights=heights, x0=0.0, y0=0.0, dx=10.0, dy=10.0)
    # A footprint inside the peak cell: the sampled value must not exceed the peak.
    small = Polygon([(11.0, 11.0), (19.0, 11.0), (19.0, 19.0), (11.0, 19.0)])
    assert grid.min_over(small) <= 5.0


def test_flat_terrain_solid_volume_equals_slab() -> None:
    grid = flat_grid((0.0, 0.0, 100.0, 50.0), resolution_m=10.0)
    solid = build_terrain_solid(grid, thickness_m=1.5)

    assert solid.is_watertight
    assert solid.is_winding_consistent
    assert non_manifold_edge_count(solid) == 0
    assert solid.euler_number == 2
    assert float(solid.volume) == pytest.approx(100.0 * 50.0 * 1.5, rel=1e-9)
    assert solid.bounds[0][2] == pytest.approx(-1.5)
    assert solid.bounds[1][2] == pytest.approx(0.0)


def test_terrain_solid_volume_matches_surface_integral() -> None:
    grid = plane_grid(nx=4, ny=5, slope_x=0.3, slope_y=-0.2)
    thickness = 1.0
    solid = build_terrain_solid(grid, thickness_m=thickness)
    vertices = solid.vertices
    faces = solid.faces
    top_faces = 2 * (grid.nx - 1) * (grid.ny - 1)

    integral = 0.0
    for face in faces[:top_faces]:
        a, b, c = vertices[face]
        ab = b - a
        ac = c - a
        area_xy = 0.5 * abs(ab[0] * ac[1] - ab[1] * ac[0])
        integral += area_xy * float((a[2] + b[2] + c[2]) / 3.0)

    area = (grid.bounds[2] - grid.bounds[0]) * (grid.bounds[3] - grid.bounds[1])
    assert float(solid.volume) == pytest.approx(area * thickness + integral, rel=1e-9)


def test_terrain_solid_survives_stl_roundtrip() -> None:
    grid = plane_grid(nx=6, ny=6, slope_x=0.2, slope_y=0.15)
    solid = build_terrain_solid(grid, thickness_m=1.0)
    loaded = load_stl_bytes(export_stl_bytes(solid))

    assert loaded.is_watertight
    assert non_manifold_edge_count(loaded) == 0
    assert float(loaded.volume) == pytest.approx(float(solid.volume), rel=1e-5)


def test_single_cell_terrain_solid_is_closed() -> None:
    grid = TerrainGrid(
        heights=np.array([[0.0, 2.0], [1.0, 3.0]]),
        x0=0.0,
        y0=0.0,
        dx=10.0,
        dy=10.0,
    )
    solid = build_terrain_solid(grid, thickness_m=1.0)
    assert solid.is_watertight
    assert float(solid.volume) > 0.0


def _border_gap(solid: trimesh.Trimesh, grid: TerrainGrid, thickness_m: float) -> dict[str, float]:
    """Worst height by which the skirt rises above the border profile, per side."""

    vertices = np.asarray(solid.vertices, dtype=float)
    faces = np.asarray(solid.faces, dtype=np.int64)
    x1 = grid.x0 + grid.dx * (grid.nx - 1)
    y1 = grid.y0 + grid.dy * (grid.ny - 1)
    sides = {
        "south": (1, grid.y0, 0, grid.x0, grid.dx, grid.heights[0, :]),
        "north": (1, y1, 0, grid.x0, grid.dx, grid.heights[-1, :]),
        "west": (0, grid.x0, 1, grid.y0, grid.dy, grid.heights[:, 0]),
        "east": (0, x1, 1, grid.y0, grid.dy, grid.heights[:, -1]),
    }

    gaps: dict[str, float] = {}
    for name, (plane_axis, plane_value, along_axis, start, step, profile) in sides.items():
        on_plane = np.isclose(vertices[:, plane_axis], plane_value, atol=1e-9)
        skirt = faces[on_plane[faces].all(axis=1)]
        skirt = skirt[~np.all(np.isclose(vertices[skirt, 2], -thickness_m), axis=1)]

        samples = start + step * np.arange(len(profile))
        envelope = np.full(len(profile), -np.inf)
        for face in skirt:
            along = vertices[face, along_axis]
            height = vertices[face, 2]
            order = np.argsort(along)
            covered = (samples >= along[order].min()) & (samples <= along[order].max())
            envelope[covered] = np.maximum(
                envelope[covered],
                np.interp(samples[covered], along[order], height[order]),
            )

        gaps[name] = float(np.max(envelope - profile)) if skirt.size else 0.0
    return gaps


def _bowl_grid() -> TerrainGrid:
    """High corners and a low interior, so every border edge dips in the middle."""

    heights = np.array(
        [
            [40.0, 20.0, 0.0, 20.0, 40.0],
            [20.0, 10.0, 0.0, 10.0, 20.0],
            [0.0, 0.0, 0.0, 0.0, 0.0],
            [20.0, 10.0, 0.0, 10.0, 20.0],
            [40.0, 20.0, 0.0, 20.0, 40.0],
        ],
        dtype=float,
    )
    return TerrainGrid(heights=heights, x0=0.0, y0=0.0, dx=10.0, dy=10.0)


def test_terrain_skirt_follows_the_border_profile() -> None:
    # A border that dips between two high nodes must show a vertical wall down
    # to the bottom; bridging the dip leaves a wall hanging above the ground.
    grid = _bowl_grid()
    solid = build_terrain_solid(grid, thickness_m=1.0)

    assert solid.is_watertight
    gaps = _border_gap(solid, grid, thickness_m=1.0)
    assert max(gaps.values()) == pytest.approx(0.0, abs=1e-9), gaps


def test_terrain_triangle_count_matches_the_solid() -> None:
    grid = _bowl_grid()
    solid = build_terrain_solid(grid, thickness_m=1.0)
    assert grid.triangle_count == len(solid.faces)


def test_grid_spacing_caps_the_cell_count() -> None:
    nx, ny, dx, dy, effective = grid_spacing((0.0, 0.0, 2000.0, 2000.0), 2.0, max_cells=5000)
    assert nx * ny <= 5000
    assert effective > 2.0
    assert (nx - 1) * dx == pytest.approx(2000.0)
    assert (ny - 1) * dy == pytest.approx(2000.0)
    assert max(dx, dy) == pytest.approx(effective)


class SyntheticSource:
    name = "synthetic"

    def __init__(self, func, meta=None) -> None:
        self.func = func
        self.meta = meta or {}

    def fetch(self, lons, lats, *, resolution_m, cancel_event=None, progress_cb=None):
        del resolution_m, cancel_event
        if progress_cb is not None:
            progress_cb(1, 1)
        values = np.asarray(self.func(np.asarray(lons), np.asarray(lats)), dtype=float)
        return ElevationData(values=values, meta={"source": self.name, **self.meta})


def _ramp(lons, lats):
    return 60.0 + (np.asarray(lats) - BBOX[0]) * 4000.0 + (np.asarray(lons) - BBOX[1]) * 1000.0


def test_build_terrain_grid_shifts_datum_to_zero(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    projector = projector_for_bbox(*BBOX)
    grid = build_terrain_grid(
        (0.0, 0.0, 600.0, 600.0),
        projector,
        settings=settings,
        resolution_m=20.0,
        source=SyntheticSource(_ramp),
    )

    assert grid.heights.min() == pytest.approx(0.0, abs=1e-9)
    assert grid.relief_m > 1.0
    assert grid.elevation_min_m == pytest.approx(grid.datum_offset_m)
    assert grid.elevation_max_m - grid.elevation_min_m == pytest.approx(grid.relief_m, rel=1e-6)
    assert grid.describe()["grid"] == {"nx": grid.nx, "ny": grid.ny}


def test_build_terrain_grid_fills_a_flat_water_surface(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    projector = projector_for_bbox(*BBOX)
    grid = build_terrain_grid(
        (0.0, 0.0, 600.0, 600.0),
        projector,
        settings=settings,
        resolution_m=20.0,
        source=SyntheticSource(_ramp),
        water_level_m=80.0,
        bank_clearance_m=5.0,
    )

    assert grid.datum_offset_m == pytest.approx(80.0)
    assert grid.elevation_min_m == pytest.approx(80.0)
    assert grid.heights.min() == pytest.approx(0.0, abs=1e-9)
    assert grid.describe()["water_level_m"] == pytest.approx(80.0)
    assert grid.describe()["water_filled_cells"] > 0
    assert grid.describe()["water_banks_cleared"] > 0
    codes = {issue.code for issue in grid.warnings}
    assert "TERRAIN_WATER_FILLED" in codes
    assert "TERRAIN_WATER_BANKS_CLEARED" in codes


def test_fill_enclosed_land_removes_an_island_but_keeps_the_mainland() -> None:
    heights = np.full((9, 9), 175.0)
    heights[0, :] = 180.0
    heights[4, 4] = 181.0
    count = _fill_enclosed_land(
        heights,
        175.0,
        core_bounds=(0.0, 0.0, 8.0, 8.0),
        x0=0.0,
        y0=0.0,
        dx=1.0,
        dy=1.0,
    )
    assert count == 1
    assert heights[4, 4] == pytest.approx(175.0)
    assert heights[0, 0] == pytest.approx(180.0)


def test_build_terrain_grid_fills_missing_cells(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    projector = projector_for_bbox(*BBOX)

    def sparse(lons, lats):
        values = _ramp(lons, lats)
        values = np.array(values, dtype=float)
        values[::7] = np.nan
        return values

    grid = build_terrain_grid(
        (0.0, 0.0, 400.0, 400.0),
        projector,
        settings=settings,
        resolution_m=20.0,
        source=SyntheticSource(sparse),
    )

    assert np.isfinite(grid.heights).all()
    codes = {issue.code for issue in grid.warnings}
    assert "TERRAIN_NODATA_FILLED" in codes


def test_build_terrain_grid_rejects_a_fully_empty_source(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    projector = projector_for_bbox(*BBOX)

    def empty(lons, lats):
        return np.full(np.asarray(lons).shape, np.nan)

    with pytest.raises(TerrainDataError):
        build_terrain_grid(
            (0.0, 0.0, 200.0, 200.0),
            projector,
            settings=settings,
            resolution_m=20.0,
            source=SyntheticSource(empty),
        )


def test_flat_relief_is_reported(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    projector = projector_for_bbox(*BBOX)
    grid = build_terrain_grid(
        (0.0, 0.0, 200.0, 200.0),
        projector,
        settings=settings,
        resolution_m=20.0,
        source=SyntheticSource(lambda lons, lats: np.full(np.asarray(lons).shape, 42.0)),
    )
    assert {"TERRAIN_FLAT_RELIEF"} <= {issue.code for issue in grid.warnings}


def test_smooth_heights_keeps_a_ramp_and_drops_a_spike() -> None:
    ny, nx = 60, 60
    yy, xx = np.mgrid[0:ny, 0:nx]
    ramp = 50.0 + 0.4 * xx + 0.2 * yy

    smoothed = smooth_heights(ramp, 25.0)
    assert float(np.abs(smoothed - ramp).max()) <= 0.4 + 1e-9

    spiky = ramp.copy()
    spiky[30, 30] += 40.0
    smoothed = smooth_heights(spiky, 25.0)
    assert abs(float(smoothed[30, 30] - ramp[30, 30])) < 1.0
    assert float(np.abs(smoothed - ramp).max()) < 1.0


def test_build_terrain_grid_suppresses_a_single_cell_spike(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    projector = projector_for_bbox(*BBOX)
    centre_lon, centre_lat = projector.local_many_to_wgs84(
        np.array([300.0]), np.array([300.0])
    )

    def spiky(lons, lats):
        values = _ramp(lons, lats)
        hit = (np.abs(np.asarray(lons) - float(centre_lon[0])) < 4e-5) & (
            np.abs(np.asarray(lats) - float(centre_lat[0])) < 4e-5
        )
        return values + 40.0 * hit

    raw_relief = float(np.ptp(_ramp(*projector.local_many_to_wgs84(
        np.array([0.0, 600.0]), np.array([0.0, 600.0])
    ))))
    grid = build_terrain_grid(
        (0.0, 0.0, 600.0, 600.0),
        projector,
        settings=settings,
        resolution_m=25.0,
        source=SyntheticSource(spiky),
    )

    assert grid.relief_m < raw_relief + 5.0
    assert grid.relief_m > 10.0
    assert grid.meta["smoothing_m"] >= 100.0
    assert "median" in grid.meta["filter"]


def test_building_box_on_a_slope_stays_attached() -> None:
    grid = plane_grid(nx=11, ny=11, dx=20.0, dy=20.0, slope_x=0.25, slope_y=0.05)
    footprint = Polygon([(60.0, 60.0), (100.0, 60.0), (100.0, 100.0), (60.0, 100.0)])
    ground = grid.min_over(footprint)

    building = trimesh.creation.box(extents=(40.0, 40.0, 18.0))
    building.apply_translation((80.0, 80.0, ground - 0.5 + 9.0))

    solid = build_terrain_solid(grid, thickness_m=1.0)
    fused = trimesh.boolean.union([building, solid], engine="manifold")
    loaded = load_stl_bytes(export_stl_bytes(fused))

    assert loaded.is_watertight
    assert non_manifold_edge_count(loaded) == 0
    # Every corner of the footprint sits above the building bottom.
    corners = np.asarray(footprint.exterior.coords[:-1], dtype=float)
    assert np.all(grid.surface_z(corners[:, 0], corners[:, 1]) > ground - 0.5)

@pytest.mark.parametrize("bounds", [(0.0, 0.0, 0.0, 10.0), (0.0, 0.0, 10.0, 0.0)])
def test_grid_spacing_rejects_empty_bounds(bounds) -> None:
    with pytest.raises(GeometryError):
        grid_spacing(bounds, 10.0)


# -- edge taper -------------------------------------------------------


def _taper_grid(tmp_path, *, taper: float, core=(0.0, 0.0, 400.0, 400.0)):
    settings = Settings(data_dir=tmp_path)
    projector = projector_for_bbox(*BBOX)
    bounds = (-200.0, -200.0, 600.0, 600.0)
    return build_terrain_grid(
        bounds,
        projector,
        settings=settings,
        resolution_m=25.0,
        source=SyntheticSource(_ramp),
        core_bounds=core,
        edge_taper_m=taper,
    )


def test_edge_taper_factor_is_one_inside_and_zero_beyond_the_band() -> None:
    core = (0.0, 0.0, 100.0, 100.0)
    assert edge_taper_factor(50.0, 50.0, core, 40.0) == pytest.approx(1.0)
    assert edge_taper_factor(0.0, 0.0, core, 40.0) == pytest.approx(1.0)
    assert edge_taper_factor(140.0, 50.0, core, 40.0) == pytest.approx(0.0)
    # The corner is measured diagonally, so it fades sooner than the flat sides.
    assert 0.0 < float(edge_taper_factor(120.0, 120.0, core, 40.0)) < 0.5
    assert edge_taper_factor(140.0, 140.0, core, 40.0) == pytest.approx(0.0)
    # Halfway through the band the smoothstep sits at 0.5.
    assert edge_taper_factor(120.0, 50.0, core, 40.0) == pytest.approx(0.5)


def test_edge_taper_leaves_the_core_untouched(tmp_path) -> None:
    plain = _taper_grid(tmp_path, taper=0.0)
    tapered = _taper_grid(tmp_path, taper=200.0)

    xs = plain.x0 + plain.dx * np.arange(plain.nx)
    ys = plain.y0 + plain.dy * np.arange(plain.ny)
    inside = (
        (xs[None, :] >= 0.0) & (xs[None, :] <= 400.0) & (ys[:, None] >= 0.0) & (ys[:, None] <= 400.0)
    )
    assert inside.any()
    assert np.allclose(tapered.heights[inside], plain.heights[inside])
    assert tapered.heights.shape == plain.heights.shape


def test_edge_taper_reaches_zero_on_the_outer_ring(tmp_path) -> None:
    grid = _taper_grid(tmp_path, taper=200.0)
    ring = np.concatenate(
        [grid.heights[0, :], grid.heights[-1, :], grid.heights[:, 0], grid.heights[:, -1]]
    )
    assert np.allclose(ring, 0.0, atol=1e-6)


def test_edge_taper_falls_monotonically_outwards(tmp_path) -> None:
    grid = _taper_grid(tmp_path, taper=200.0)
    column = grid.heights[:, grid.nx // 2]
    ys = grid.y0 + grid.dy * np.arange(grid.ny)
    band = ys >= 400.0
    assert np.all(np.diff(column[band]) <= 1e-6)


def test_edge_taper_requires_core_bounds(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    projector = projector_for_bbox(*BBOX)
    with pytest.raises(GeometryError):
        build_terrain_grid(
            (0.0, 0.0, 400.0, 400.0),
            projector,
            settings=settings,
            resolution_m=25.0,
            source=SyntheticSource(_ramp),
            edge_taper_m=100.0,
        )


def test_edge_taper_leaves_a_residue_when_the_frame_is_too_tight(tmp_path) -> None:
    """A frame narrower than the band keeps a residue: the caller must grow it.

    ``run_pipeline`` always grows the terrain bounds by ``edge_taper_m``, so this
    only documents the clamp for direct callers.
    """

    settings = Settings(data_dir=tmp_path)
    projector = projector_for_bbox(*BBOX)
    grid = build_terrain_grid(
        (-50.0, -50.0, 450.0, 450.0),
        projector,
        settings=settings,
        resolution_m=25.0,
        source=SyntheticSource(_ramp),
        core_bounds=(0.0, 0.0, 400.0, 400.0),
        edge_taper_m=200.0,
    )
    ring = np.concatenate(
        [grid.heights[0, :], grid.heights[-1, :], grid.heights[:, 0], grid.heights[:, -1]]
    )
    assert 0.0 < float(ring.max()) < float(grid.heights.max())



