"""Local terrain height field and the closed terrain solid.

The height field is a regular grid in the local metric frame. Sampling and the
solid use the same fixed diagonal split per cell, so a building base computed
here always lies below the exported terrain surface.
"""

from __future__ import annotations

import threading
import warnings
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import trimesh
from shapely.geometry import Polygon

from app.config import Settings
from app.services.errors import GeometryError, TerrainDataError
from app.services.issues import Issue
from app.services.projection import Projector

DEFAULT_RESOLUTION_M = 30.0
MIN_RESOLUTION_M = 5.0
MAX_RESOLUTION_M = 50.0
MAX_TERRAIN_CELLS = 80_000
# The source DEM is a ~30 m surface model: it contains buildings, trees, and
# radar spikes. A running median over ~150 m removes that noise while keeping
# real landforms (a 250 m mountain loses < 3% of its relief in tests).
DEFAULT_SMOOTHING_M = 150.0
MAX_MEDIAN_CELLS = 31
STEEP_FOOTPRINT_RELIEF_M = 3.0
FLAT_RELIEF_M = 0.5
NODATA_LIMIT_M = -1000.0

ProgressCallback = Callable[[int, int], None]


@dataclass
class ElevationData:
    """Raw elevations sampled at the requested points.

    ``values`` uses NaN for missing data; the caller fills the grid because
    only the caller knows the sampling layout.
    """

    values: np.ndarray
    meta: dict[str, Any] = field(default_factory=dict)


class ElevationSource(Protocol):
    """Anything that can return elevations (metres) for lon/lat points."""

    name: str

    def fetch(
        self,
        lons: np.ndarray,
        lats: np.ndarray,
        *,
        resolution_m: float,
        cancel_event: threading.Event | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> ElevationData: ...


@dataclass
class TerrainGrid:
    """A regular height field in the local metric frame (``+X`` east, ``+Y`` north)."""

    heights: np.ndarray
    x0: float
    y0: float
    dx: float
    dy: float
    datum_offset_m: float = 0.0
    elevation_min_m: float = 0.0
    elevation_max_m: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def nx(self) -> int:
        return int(self.heights.shape[1])

    @property
    def ny(self) -> int:
        return int(self.heights.shape[0])

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            float(self.x0),
            float(self.y0),
            float(self.x0 + self.dx * (self.nx - 1)),
            float(self.y0 + self.dy * (self.ny - 1)),
        )

    @property
    def relief_m(self) -> float:
        if self.heights.size == 0:
            return 0.0
        return float(self.heights.max() - self.heights.min())

    @property
    def resolution_m(self) -> float:
        return float(max(self.dx, self.dy))

    @property
    def triangle_count(self) -> int:
        border = 2 * (self.nx + self.ny - 2)
        return 2 * (self.nx - 1) * (self.ny - 1) + 2 * border + (border - 2)

    # -- sampling ------------------------------------------------------
    def _cell_terms(
        self, x: np.ndarray | float, y: np.ndarray | float
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Cell indexes and fractional position, matching the solid's split."""

        u, v = np.broadcast_arrays(
            (np.asarray(x, dtype=float) - self.x0) / self.dx,
            (np.asarray(y, dtype=float) - self.y0) / self.dy,
        )
        i = np.clip(np.floor(u).astype(np.int64), 0, self.nx - 2)
        j = np.clip(np.floor(v).astype(np.int64), 0, self.ny - 2)
        fu = np.clip(u - i, 0.0, 1.0)
        fv = np.clip(v - j, 0.0, 1.0)
        return i, j, fu, fv

    def surface_z(self, x: np.ndarray | float, y: np.ndarray | float) -> np.ndarray:
        """Exact height on the triangulated surface (same split as the solid)."""

        i, j, fu, fv = self._cell_terms(x, y)

        h00 = self.heights[j, i]
        h10 = self.heights[j, i + 1]
        h01 = self.heights[j + 1, i]
        h11 = self.heights[j + 1, i + 1]
        # Cell split: (v00, v10, v11) below the diagonal, (v00, v11, v01) above.
        lower = h00 + (h10 - h00) * fu + (h11 - h10) * fv
        upper = h00 + (h11 - h01) * fu + (h01 - h00) * fv
        return np.where(fu >= fv, lower, upper)

    def surface_normal(
        self, x: np.ndarray | float, y: np.ndarray | float
    ) -> np.ndarray:
        """Unit normal of the triangulated surface at ``(x, y)``, ``+z`` up."""

        i, j, fu, fv = self._cell_terms(x, y)

        h00 = self.heights[j, i]
        h10 = self.heights[j, i + 1]
        h01 = self.heights[j + 1, i]
        h11 = self.heights[j + 1, i + 1]
        lower = fu >= fv
        gx = np.where(lower, (h10 - h00) / self.dx, (h11 - h01) / self.dx)
        gy = np.where(lower, (h11 - h10) / self.dy, (h01 - h00) / self.dy)
        norm = np.sqrt(gx * gx + gy * gy + 1.0)
        return np.stack([-gx / norm, -gy / norm, 1.0 / norm], axis=-1)

    def _cell_range(
        self, polygon: Polygon
    ) -> tuple[int, int, int, int]:
        minx, miny, maxx, maxy = (float(value) for value in polygon.bounds)
        i0 = int(np.floor((minx - self.x0) / self.dx))
        i1 = int(np.ceil((maxx - self.x0) / self.dx))
        j0 = int(np.floor((miny - self.y0) / self.dy))
        j1 = int(np.ceil((maxy - self.y0) / self.dy))
        return (
            min(max(i0, 0), self.nx - 1),
            min(max(j0, 0), self.ny - 1),
            min(max(i1, 0), self.nx - 1),
            min(max(j1, 0), self.ny - 1),
        )

    def min_over(self, polygon: Polygon) -> float:
        """Lowest terrain height under a footprint.

        Every node of the covering cells is included, so the result is at or
        below the true minimum over the footprint. That is the safe direction:
        a slightly too-low base is buried by the union, a too-high base floats.
        """

        if self.heights.size == 0:
            return 0.0
        i0, j0, i1, j1 = self._cell_range(polygon)
        patch = self.heights[j0 : j1 + 1, i0 : i1 + 1]
        best = float(patch.min()) if patch.size else float(self.heights.min())

        rings = [polygon.exterior, *polygon.interiors]
        points = [np.asarray(ring.coords, dtype=float) for ring in rings if len(ring.coords)]
        centroid = polygon.representative_point()
        # One vectorised surface_z call for the rings plus the representative
        # point: two calls per footprint is a measurable share of the build.
        if points:
            coords = np.concatenate(points, axis=0)
            x = np.append(coords[:, 0], centroid.x)
            y = np.append(coords[:, 1], centroid.y)
        else:
            x = np.asarray([centroid.x], dtype=float)
            y = np.asarray([centroid.y], dtype=float)
        values = self.surface_z(x, y)
        if values.size:
            best = min(best, float(np.nanmin(values)))
        return best

    def relief_over(self, polygon: Polygon) -> float:
        """Approximate height range under a footprint (for diagnostics)."""

        i0, j0, i1, j1 = self._cell_range(polygon)
        patch = self.heights[j0 : j1 + 1, i0 : i1 + 1]
        if patch.size < 2:
            return 0.0
        return float(patch.max() - patch.min())

    def describe(self) -> dict[str, Any]:
        meta = dict(self.meta)
        meta.update(
            {
                "resolution_m": round(self.resolution_m, 3),
                "grid": {"nx": self.nx, "ny": self.ny},
                "datum_offset_m": round(float(self.datum_offset_m), 3),
                "elevation_min_m": round(float(self.elevation_min_m), 3),
                "elevation_max_m": round(float(self.elevation_max_m), 3),
                "relief_m": round(self.relief_m, 3),
                "triangle_count": self.triangle_count,
                "vertical_datum": "source DEM, metres above sea level",
                "z_reference": "lowest terrain cell in the selection",
            }
        )
        return meta


def edge_taper_factor(
    x: np.ndarray | float,
    y: np.ndarray | float,
    core_bounds: tuple[float, float, float, float],
    taper_m: float,
) -> np.ndarray:
    """Smooth 1 -> 0 weight outside ``core_bounds``, reaching 0 at ``taper_m``.

    The terrain block's side faces are vertical walls whose height is the DEM
    height on the frame border; inside a CFD domain that reads as a cliff at the
    inlet. Multiplying the height field by this factor turns the ring outside the
    selection into a ramp that meets ``z = 0`` at the frame, so the model sits
    flush with the ground plane while the selected area keeps its true relief.
    """

    if taper_m <= 0.0:
        return np.ones_like(np.asarray(x, dtype=float))
    minx, miny, maxx, maxy = (float(value) for value in core_bounds)
    x_arr = np.asarray(x, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    # Euclidean distance to the rectangle: zero inside, growing outwards.
    dx_out = np.maximum(np.maximum(minx - x_arr, x_arr - maxx), 0.0)
    dy_out = np.maximum(np.maximum(miny - y_arr, y_arr - maxy), 0.0)
    distance = np.hypot(dx_out, dy_out)
    t = np.clip(distance / float(taper_m), 0.0, 1.0)
    return 1.0 - t * t * (3.0 - 2.0 * t)


# -- grid construction -------------------------------------------------


def grid_spacing(
    bounds: tuple[float, float, float, float],
    resolution_m: float,
    max_cells: int = MAX_TERRAIN_CELLS,
) -> tuple[int, int, float, float, float]:
    """Return ``(nx, ny, dx, dy, effective_resolution)`` covering the bounds."""

    minx, miny, maxx, maxy = (float(value) for value in bounds)
    width = maxx - minx
    height = maxy - miny
    if width <= 0.0 or height <= 0.0:
        raise GeometryError("地形范围无效", "地形范围必须具有正宽度和正高度")

    resolution = float(resolution_m)
    if resolution < MIN_RESOLUTION_M:
        resolution = MIN_RESOLUTION_M

    for _ in range(200):
        nx = max(2, int(np.ceil(width / resolution)) + 1)
        ny = max(2, int(np.ceil(height / resolution)) + 1)
        if nx * ny <= max_cells or resolution > max(width, height):
            dx = width / (nx - 1)
            dy = height / (ny - 1)
            return nx, ny, dx, dy, float(max(dx, dy))
        resolution *= 1.15

    raise GeometryError("地形网格过密", "无法在节点数上限内生成地形网格")


def _fill_nodata(heights: np.ndarray) -> tuple[np.ndarray, int]:
    """Fill NaN cells from the nearest valid neighbours; return (grid, filled count)."""

    mask = ~np.isfinite(heights)
    missing = int(mask.sum())
    if missing == 0:
        return heights, 0
    if missing == heights.size:
        raise TerrainDataError(
            "该区域没有可用的地形高程数据",
            "DEM 瓦片全部为空值，请换一个区域或关闭地形",
        )

    filled = np.where(mask, np.nan, heights).astype(float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for _ in range(heights.size + 1):
            if not np.isnan(filled).any():
                break
            padded = np.pad(filled, 1, mode="constant", constant_values=np.nan)
            neighbours = np.stack(
                [
                    padded[:-2, 1:-1],
                    padded[2:, 1:-1],
                    padded[1:-1, :-2],
                    padded[1:-1, 2:],
                ]
            )
            mean = np.nanmean(neighbours, axis=0)
            update = np.isnan(filled) & np.isfinite(mean)
            if not update.any():
                break
            filled[update] = mean[update]

    if np.isnan(filled).any():
        filled = np.where(np.isnan(filled), float(np.nanmean(filled)), filled)
    return filled, missing


def _median_filter(heights: np.ndarray, kernel: int) -> np.ndarray:
    """Exact 2-D running median with edge padding, processed in row strips."""

    if kernel <= 1:
        return heights.copy()

    pad = kernel // 2
    padded = np.pad(heights, pad, mode="edge")
    ny, nx = heights.shape
    result = np.empty_like(heights)
    rows = max(1, 2_000_000 // max(nx * kernel * kernel, 1))
    for start in range(0, ny, rows):
        stop = min(ny, start + rows)
        windows = np.lib.stride_tricks.sliding_window_view(
            padded[start : stop + kernel - 1],
            (kernel, kernel),
        )
        result[start:stop] = np.median(windows, axis=(-1, -2))
    return result


def smooth_heights(
    heights: np.ndarray,
    spacing_m: float,
    *,
    smoothing_m: float = DEFAULT_SMOOTHING_M,
) -> np.ndarray:
    """Suppress surface-model noise (buildings, trees, radar spikes).

    The source DEM is not a bare-earth model: in dense areas it follows roofs
    and canopy, and single 30 m cells can be off by tens of metres. A running
    median over ``smoothing_m`` removes those features while preserving linear
    slopes and large landforms.
    """

    if smoothing_m <= 0.0 or spacing_m <= 0.0:
        return heights.copy()
    kernel = int(round(float(smoothing_m) / float(spacing_m))) | 1
    kernel = min(max(kernel, 3), MAX_MEDIAN_CELLS)
    return _median_filter(heights, kernel)


def _fill_enclosed_land(
    heights: np.ndarray,
    water_level: float,
    *,
    core_bounds: tuple[float, float, float, float] | None,
    x0: float,
    y0: float,
    dx: float,
    dy: float,
) -> int:
    """Lower land components that do not touch the core border.

    With a flat water surface, a shoal or island is a land component surrounded
    by water. The mainland always reaches the selection border, so a four-neighbour
    flood fill from the core border separates the two. Returns the cell count.
    """

    ny, nx = heights.shape
    land = heights > (water_level + 1e-6)
    if not land.any():
        return 0

    if core_bounds is not None:
        minx, miny, maxx, maxy = core_bounds
        i0 = max(0, min(nx - 1, int(np.floor((minx - x0) / dx))))
        i1 = max(0, min(nx - 1, int(np.ceil((maxx - x0) / dx))))
        j0 = max(0, min(ny - 1, int(np.floor((miny - y0) / dy))))
        j1 = max(0, min(ny - 1, int(np.ceil((maxy - y0) / dy))))
    else:
        i0, i1, j0, j1 = 0, nx - 1, 0, ny - 1

    mainland = np.zeros_like(land, dtype=bool)
    queue: deque[tuple[int, int]] = deque()

    def seed(y: int, x: int) -> None:
        if land[y, x] and not mainland[y, x]:
            mainland[y, x] = True
            queue.append((y, x))

    for x in range(i0, i1 + 1):
        seed(j0, x)
        seed(j1, x)
    for y in range(j0, j1 + 1):
        seed(y, i0)
        seed(y, i1)
    if not queue:
        # The selection border is entirely water; do not guess which blob is land.
        return 0

    while queue:
        y, x = queue.popleft()
        for dy_step, dx_step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            yy, xx = y + dy_step, x + dx_step
            if (
                j0 <= yy <= j1
                and i0 <= xx <= i1
                and land[yy, xx]
                and not mainland[yy, xx]
            ):
                mainland[yy, xx] = True
                queue.append((yy, xx))

    core = np.zeros_like(land, dtype=bool)
    core[j0 : j1 + 1, i0 : i1 + 1] = True
    islands = land & core & ~mainland
    count = int(islands.sum())
    if count:
        heights[islands] = water_level
    return count


def build_terrain_grid(
    bounds: tuple[float, float, float, float],
    projector: Projector,
    *,
    settings: Settings,
    resolution_m: float = DEFAULT_RESOLUTION_M,
    source: ElevationSource | None = None,
    cancel_event: threading.Event | None = None,
    progress_cb: ProgressCallback | None = None,
    core_bounds: tuple[float, float, float, float] | None = None,
    edge_taper_m: float = 0.0,
    water_level_m: float | None = None,
    fill_water_islands: bool = False,
    bank_clearance_m: float = 0.0,
) -> TerrainGrid:
    """Sample the DEM on a local grid and return a datum-shifted height field.

    ``bounds`` is the grid extent. When ``edge_taper_m`` is positive, heights
    outside ``core_bounds`` fade towards the datum over that many metres, so the
    block's border sits at ``z = 0`` instead of ending in a tall vertical wall.
    The caller must grow ``bounds`` by ``edge_taper_m``; a tighter frame only
    gets part-way down, because the weight is clamped at one band width.

    ``water_level_m`` is an absolute DEM elevation. Cells below it are raised to
    it before the datum shift, which turns a river channel into a flat water
    surface at ``z = 0`` (the same flat-plane convention the flat cities get from
    their slab). ``None`` keeps the raw DEM channel. ``fill_water_islands`` also
    lowers land components that are fully enclosed by that surface, so a shoal
    does not sit as a gray bump in the river. ``bank_clearance_m`` clears every
    cell below ``water_level_m + bank_clearance_m``, which also removes low banks
    and peninsulas connected to the shore.
    """

    if edge_taper_m > 0.0 and core_bounds is None:
        raise GeometryError("地形边缘缓冲需要核心范围", "core_bounds is required when edge_taper_m > 0")

    nx, ny, dx, dy, effective = grid_spacing(
        bounds,
        resolution_m,
        max(4, int(settings.max_terrain_cells)),
    )
    if cancel_event is not None and cancel_event.is_set():
        raise _cancelled()

    minx, miny, _, _ = (float(value) for value in bounds)
    kernel = int(round(DEFAULT_SMOOTHING_M / float(dx))) | 1
    kernel = min(max(kernel, 3), MAX_MEDIAN_CELLS)
    # Sample a margin, smooth, then crop: the median filter needs real
    # neighbours at the region border, and edge padding would flatten it.
    pad = kernel // 2
    sample_nx = nx + 2 * pad
    sample_ny = ny + 2 * pad
    xs = (minx - pad * dx) + dx * np.arange(sample_nx)
    ys = (miny - pad * dy) + dy * np.arange(sample_ny)
    grid_x, grid_y = np.meshgrid(xs, ys)
    lons, lats = projector.local_many_to_wgs84(grid_x.ravel(), grid_y.ravel())

    if source is None:
        from app.services.dem import TerrariumSource

        source = TerrariumSource(settings)

    data = source.fetch(
        lons,
        lats,
        resolution_m=float(resolution_m),
        cancel_event=cancel_event,
        progress_cb=progress_cb,
    )
    if cancel_event is not None and cancel_event.is_set():
        raise _cancelled()

    raw = np.asarray(data.values, dtype=float)
    if raw.size != sample_nx * sample_ny:
        raise TerrainDataError(
            "地形高程数据长度不匹配",
            f"expected={sample_nx * sample_ny}, received={raw.size}",
        )
    raw = raw.reshape(sample_ny, sample_nx)
    filled = int(np.isnan(raw[pad : pad + ny, pad : pad + nx]).sum())
    heights, _ = _fill_nodata(raw)
    heights = _median_filter(heights, kernel)[pad : pad + ny, pad : pad + nx]

    water_filled = 0
    if water_level_m is not None:
        water_level = float(water_level_m)
        water_filled = int((heights < water_level).sum())
        heights = np.maximum(heights, water_level)

    water_banks_cleared = 0
    if water_level_m is not None and bank_clearance_m > 0.0:
        ceiling = float(water_level_m) + float(bank_clearance_m)
        bank_mask = heights < ceiling
        water_banks_cleared = int(bank_mask.sum())
        if water_banks_cleared:
            heights[bank_mask] = float(water_level_m)

    water_islands_filled = 0
    if water_level_m is not None and fill_water_islands:
        water_islands_filled = _fill_enclosed_land(
            heights,
            float(water_level_m),
            core_bounds=core_bounds,
            x0=minx,
            y0=miny,
            dx=dx,
            dy=dy,
        )

    datum = float(heights.min())
    local = heights - datum
    if edge_taper_m > 0.0:
        xs = minx + dx * np.arange(nx)
        ys = miny + dy * np.arange(ny)
        grid_x, grid_y = np.meshgrid(xs, ys)
        local = local * edge_taper_factor(grid_x, grid_y, core_bounds, float(edge_taper_m))
    meta = dict(data.meta)
    meta["nodata_filled_cells"] = filled
    meta["smoothing_m"] = round(kernel * float(dx), 1)
    meta["filter"] = f"median {kernel}x{kernel} cells"
    if edge_taper_m > 0.0:
        meta["edge_taper_m"] = round(float(edge_taper_m), 3)
        meta["edge_taper_core_bounds"] = [round(float(value), 3) for value in core_bounds]
    if water_level_m is not None:
        meta["water_level_m"] = round(float(water_level_m), 3)
        meta["water_filled_cells"] = water_filled
        meta["water_bank_clearance_m"] = round(float(bank_clearance_m), 3)
        meta["water_banks_cleared"] = water_banks_cleared
        meta["water_islands_filled"] = water_islands_filled
    grid = TerrainGrid(
        heights=local,
        x0=minx,
        y0=miny,
        dx=dx,
        dy=dy,
        datum_offset_m=datum,
        elevation_min_m=datum,
        elevation_max_m=float(datum + local.max()),
        meta=meta,
    )

    grid.warnings.extend(_terrain_warnings(grid, data.meta, filled, float(resolution_m), effective))
    if water_level_m is not None and water_filled:
        grid.warnings.append(
            Issue(
                code="TERRAIN_WATER_FILLED",
                message=(
                    f"{water_filled} 个地形网格点低于水面 {float(water_level_m):g} m，"
                    "已抬到水面（河道按平地处理）"
                ),
                count=water_filled,
            )
        )
    if water_banks_cleared:
        grid.warnings.append(
            Issue(
                code="TERRAIN_WATER_BANKS_CLEARED",
                message=(
                    f"{water_banks_cleared} 个低于水面 +{float(bank_clearance_m):g} m 的"
                    "岸坡格点已压到水面（低岸/半岛按平地处理）"
                ),
                count=water_banks_cleared,
            )
        )
    if water_islands_filled:
        grid.warnings.append(
            Issue(
                code="TERRAIN_WATER_ISLANDS_FILLED",
                message=(
                    f"{water_islands_filled} 个被水面包围的陆地块已压到水面"
                    "（沙洲/小岛按平地处理）"
                ),
                count=water_islands_filled,
            )
        )
    return grid


def _cancelled() -> Exception:
    from app.services.errors import JobCancelled

    return JobCancelled("任务已取消")


def _terrain_warnings(
    grid: TerrainGrid,
    meta: dict[str, Any],
    filled: int,
    requested_resolution: float,
    effective_resolution: float,
) -> list[Issue]:
    issues: list[Issue] = []
    if effective_resolution > requested_resolution * 1.001:
        issues.append(
            Issue(
                code="TERRAIN_RESOLUTION_REDUCED",
                message=(
                    f"地形网格已从 {requested_resolution:g} m 加粗到 "
                    f"{effective_resolution:.1f} m 以控制三角形数量"
                ),
            )
        )
    pixel = meta.get("pixel_m")
    if meta.get("limited_by_max_zoom") and pixel:
        issues.append(
            Issue(
                code="TERRAIN_RESOLUTION_LIMITED",
                message=(
                    f"DEM 瓦片最高缩放级别的像元约 {float(pixel):.1f} m，"
                    f"比请求的 {requested_resolution:g} m 粗"
                ),
            )
        )
    if meta.get("zoom_reduced"):
        issues.append(
            Issue(
                code="TERRAIN_ZOOM_REDUCED",
                message="为控制瓦片数量已降低 DEM 缩放级别，地形细节相应减少",
            )
        )
    if filled:
        issues.append(
            Issue(
                code="TERRAIN_NODATA_FILLED",
                message=f"{filled} 个地形网格点缺少高程数据，已用邻近值填充",
                count=filled,
            )
        )
    if grid.relief_m < FLAT_RELIEF_M:
        issues.append(
            Issue(
                code="TERRAIN_FLAT_RELIEF",
                message=f"所选范围高差仅 {grid.relief_m:.2f} m，结果接近平地",
            )
        )
    return issues


def flat_grid(
    bounds: tuple[float, float, float, float],
    resolution_m: float = DEFAULT_RESOLUTION_M,
) -> TerrainGrid:
    """A zero-relief grid (tests and local checks)."""

    nx, ny, dx, dy, _ = grid_spacing(bounds, resolution_m)
    minx, miny, _, _ = (float(value) for value in bounds)
    return TerrainGrid(
        heights=np.zeros((ny, nx), dtype=float),
        x0=minx,
        y0=miny,
        dx=dx,
        dy=dy,
        meta={"source": "flat", "tiles": 0, "cached_tiles": 0},
    )


# -- solid construction ------------------------------------------------


def build_terrain_solid(
    grid: TerrainGrid,
    thickness_m: float = 1.0,
) -> trimesh.Trimesh:
    """Build a closed solid: terrain top, vertical skirt, flat bottom."""

    if thickness_m <= 0.0:
        raise GeometryError("地形底座厚度必须为正")

    ny, nx = grid.heights.shape
    xs = grid.x0 + grid.dx * np.arange(nx)
    ys = grid.y0 + grid.dy * np.arange(ny)
    grid_x, grid_y = np.meshgrid(xs, ys)
    top = np.column_stack(
        [grid_x.ravel(), grid_y.ravel(), grid.heights.ravel().astype(float)]
    )

    index = np.arange(nx * ny, dtype=np.int64).reshape(ny, nx)
    # Border ring, counter-clockwise seen from +Z: south (west -> east), east
    # (south -> north), north (east -> west), west (north -> south). Every ring
    # node gets its own bottom vertex so the skirt is a strip of vertical quads.
    # A single fan anchored on one corner would bridge any concave stretch of
    # the border profile and leave a wall hanging above the low ground.
    ring = np.concatenate(
        [
            index[0, :-1],
            index[:-1, -1],
            index[-1, :0:-1],
            index[:0:-1, 0],
        ]
    )
    count = nx * ny
    bottom_ring = count + np.arange(ring.size, dtype=np.int64)
    bottom = np.column_stack(
        [
            top[ring, 0],
            top[ring, 1],
            np.full(ring.size, -float(thickness_m)),
        ]
    )
    vertices = np.vstack([top, bottom])

    v00 = index[:-1, :-1].ravel()
    v10 = index[:-1, 1:].ravel()
    v11 = index[1:, 1:].ravel()
    v01 = index[1:, :-1].ravel()
    faces: list[tuple[int, int, int]] = [
        *zip(v00.tolist(), v10.tolist(), v11.tolist(), strict=True),
        *zip(v00.tolist(), v11.tolist(), v01.tolist(), strict=True),
    ]

    # Skirt: two outward-facing triangles per border segment.
    ring_list = ring.tolist()
    bottom_list = bottom_ring.tolist()
    last = ring.size - 1
    for k, top_index in enumerate(ring_list):
        next_k = 0 if k == last else k + 1
        here = bottom_list[k]
        next_here = bottom_list[next_k]
        faces.append((top_index, here, next_here))
        faces.append((top_index, next_here, ring_list[next_k]))

    # Flat bottom, fanning from the south-west corner along the same ring.
    anchor = bottom_list[0]
    for k in range(1, last):
        faces.append((anchor, bottom_list[k + 1], bottom_list[k]))

    mesh = trimesh.Trimesh(
        vertices=vertices,
        faces=np.asarray(faces, dtype=np.int64),
        process=False,
    )
    trimesh.repair.fix_normals(mesh)
    if not (
        mesh.is_watertight
        and mesh.is_winding_consistent
        and float(mesh.volume) > 0.0
        and np.isfinite(mesh.vertices).all()
    ):
        raise GeometryError("地形实体构造失败", "地形网格未形成闭合实体")
    return mesh
