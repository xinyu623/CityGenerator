"""Fuse building shells and a solid base into one watertight city mesh."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import trimesh
from trimesh.creation import box

from app.services.errors import CityUnionError
from app.services.stl_export import (
    export_stl_bytes,
    load_stl_bytes,
    non_manifold_edge_count,
)

MANIFOLD_ENGINE = "manifold"
MAX_WELD_PASSES = 8
WELD_CLUSTER_RADIUS_M = 0.5
WELD_PAD_M = 0.4
# Binary STL stores float32 coordinates: at a 4 km extent two vertices less than
# ~0.5 mm apart round to the same point, which turns their long edges into edges
# shared by four faces after the file is read back. Nudge one vertex of such a
# pair by COLLAPSE_SHIFT_M so the round-trip keeps the original topology.
COLLAPSE_TOLERANCE_M = 5e-4
COLLAPSE_SHIFT_M = 1e-3


def separate_collapsed_vertices(
    mesh: trimesh.Trimesh,
    tolerance_m: float = COLLAPSE_TOLERANCE_M,
    shift_m: float = COLLAPSE_SHIFT_M,
) -> int:
    """Move one vertex of every sub-tolerance pair; return how many moved.

    The mesh stays topologically identical (watertight, manifold); only a few
    vertices shift by about a millimetre, far below any modelling scale.

    Vertices are bucketed into cells of ``tolerance_m`` and every vertex scans
    all 27 neighbouring cells. Scanning only the 14 "forward" directions would
    miss a pair whose lower index sits in the higher cell. Packing the cell
    coordinates into a Python int keeps the lookup at one dict access per
    direction; at 0.5 mm over 4 km the packed key is wider than int64, which is
    why plain Python ints are used.
    """

    if tolerance_m <= 0.0 or not len(mesh.vertices):
        return 0
    vertices = mesh.vertices
    cells = np.floor(vertices / tolerance_m).astype(np.int64)
    # Shift so every cell is >= 1: a -1 neighbour then never underflows.
    cells -= cells.min(axis=0) - 1
    span = cells.max(axis=0) + 2
    stride_y = int(span[2])
    stride_x = int(span[1]) * stride_y
    buckets: dict[int, list[int]] = {}
    keys: list[int] = []
    for index, (x, y, z) in enumerate(cells.tolist()):
        key = x * stride_x + y * stride_y + z
        buckets.setdefault(key, []).append(index)
        keys.append(key)

    deltas = [
        dx * stride_x + dy * stride_y + dz
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
    ]
    shift = np.array([shift_m, shift_m, 0.0])

    moved = 0
    for index in range(len(vertices)):
        base = keys[index]
        for delta in deltas:
            for other in buckets.get(base + delta, ()):
                if other <= index:
                    continue
                if np.linalg.norm(vertices[other] - vertices[index]) < tolerance_m:
                    vertices[other] += shift
                    moved += 1
    if moved:
        mesh.vertices = vertices
    return moved


@dataclass(frozen=True)
class FusedCity:
    mesh: trimesh.Trimesh
    base_bounds: tuple[float, float, float, float]
    base_thickness_m: float
    base_mesh: trimesh.Trimesh | None = None
    engine: str = MANIFOLD_ENGINE
    weld_repairs: int = 0
    # The STL bytes fuse_city had to export to prove the mesh survives a
    # float32 round-trip, plus the mesh read back from them. The output stage
    # writes these instead of exporting and reloading the city again.
    stl_bytes: bytes | None = None
    stl_mesh: trimesh.Trimesh | None = None


def _non_manifold_edges(mesh: trimesh.Trimesh) -> np.ndarray:
    edges = np.asarray(mesh.edges_sorted)
    if not edges.size:
        return np.empty((0, 2), dtype=np.int64)
    unique, counts = np.unique(edges, axis=0, return_counts=True)
    return unique[counts > 2]


def _cluster_points(points: np.ndarray) -> list[list[int]]:
    """Group nearby contact midpoints so one patch covers a dense contact."""

    if not len(points):
        return []

    count = len(points)
    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        root_first, root_second = find(first), find(second)
        if root_first != root_second:
            parent[root_second] = root_first

    buckets: dict[tuple[int, int], list[int]] = {}
    for index, point in enumerate(points[:, :2]):
        cell = (
            int(np.floor(point[0] / WELD_CLUSTER_RADIUS_M)),
            int(np.floor(point[1] / WELD_CLUSTER_RADIUS_M)),
        )
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in buckets.get((cell[0] + dx, cell[1] + dy), ()):
                    if np.linalg.norm(point - points[other, :2]) <= WELD_CLUSTER_RADIUS_M:
                        union(index, other)
        buckets.setdefault(cell, []).append(index)

    groups: dict[int, list[int]] = {}
    for index in range(count):
        groups.setdefault(find(index), []).append(index)
    return list(groups.values())


def _weld_boxes(mesh: trimesh.Trimesh, edges: np.ndarray) -> list[trimesh.Trimesh]:
    """Add small boxes that turn edge/corner contacts into solid overlaps."""

    midpoints = np.asarray(
        [(mesh.vertices[edge[0]] + mesh.vertices[edge[1]]) / 2.0 for edge in edges]
    )
    welds = []
    for group in _cluster_points(midpoints):
        endpoints = [
            mesh.vertices[edge] for index in group for edge in (edges[index][0], edges[index][1])
        ]
        points = np.asarray(endpoints)
        minimum, maximum = points.min(axis=0), points.max(axis=0)
        size = maximum - minimum + 2.0 * WELD_PAD_M
        weld = box(extents=size)
        weld.apply_translation((minimum + maximum) / 2.0)
        welds.append(weld)
    return welds


def _closed_stl_roundtrip(mesh: trimesh.Trimesh) -> tuple[bytes, trimesh.Trimesh | None]:
    """Export ``mesh`` and read it back; return the bytes and the reloaded mesh.

    The reloaded mesh is ``None`` when the float32 round-trip lost the closure,
    which is what the weld repair pass reacts to. When it is not ``None`` the
    caller can write exactly those bytes instead of exporting again.
    """

    data = export_stl_bytes(mesh)
    loaded = load_stl_bytes(data)
    if not bool(loaded.is_watertight) or non_manifold_edge_count(loaded):
        return data, None
    return data, loaded


def validate_city(mesh: trimesh.Trimesh) -> int:
    """Raise ``CityUnionError`` unless the fused mesh is a closed solid.

    Returns the non-manifold edge count (zero on success) so callers that record
    it do not have to run the edge census a second time.
    """

    if not len(mesh.faces):
        raise CityUnionError("融合结果没有三角形")
    if not bool(mesh.is_watertight):
        raise CityUnionError("融合结果不是水密表面")
    if not bool(mesh.is_winding_consistent):
        raise CityUnionError("融合结果法向绕向不一致")
    if not np.isfinite(mesh.vertices).all():
        raise CityUnionError("融合结果包含非有限坐标")

    volume = float(mesh.volume)
    if not np.isfinite(volume) or volume <= 0.0:
        raise CityUnionError("融合结果体积无效")

    non_manifold = non_manifold_edge_count(mesh)
    if non_manifold:
        raise CityUnionError(
            f"融合结果仍有 {non_manifold} 条非流形边",
            detail="布尔并集未能形成单一流形表面",
        )
    return non_manifold


def fuse_city(
    buildings: Sequence[trimesh.Trimesh],
    base: trimesh.Trimesh,
    *,
    base_thickness_m: float | None = None,
    check_input_volumes: bool = True,
) -> FusedCity:
    """Boolean-union every building shell with the solid base.

    ``check_input_volumes`` maps to trimesh's ``check_volume``: it makes the
    engine validate every input shell (watertight, consistent winding, positive
    volume) before the union. The pipeline validates each shell itself before
    it gets here and passes ``False``; a direct caller keeps the check.
    """

    if not buildings:
        raise CityUnionError("没有可融合的建筑壳体")
    if base is None or not len(base.faces):
        raise CityUnionError("缺少实体地面")

    meshes: list[trimesh.Trimesh] = [*buildings, base]
    welds: list[trimesh.Trimesh] = []
    fused: trimesh.Trimesh | None = None
    stl_bytes: bytes | None = None
    stl_mesh: trimesh.Trimesh | None = None

    for _ in range(MAX_WELD_PASSES):
        try:
            fused = trimesh.boolean.union(
                [*meshes, *welds],
                engine=MANIFOLD_ENGINE,
                check_volume=check_input_volumes,
            )
        except Exception as exc:
            raise CityUnionError(
                "建筑与地面布尔融合失败",
                detail=str(exc),
            ) from exc

        if not isinstance(fused, trimesh.Trimesh):
            raise CityUnionError("布尔融合返回了非网格结果")
        separate_collapsed_vertices(fused)
        validate_city(fused)

        stl_bytes, stl_mesh = _closed_stl_roundtrip(fused)
        if stl_mesh is not None:
            # Validate the file's own contents once; write_outputs then reuses
            # these bytes and this mesh without repeating the census.
            validate_city(stl_mesh)
            break

        probe = fused.copy()
        probe.merge_vertices()
        edges = _non_manifold_edges(probe)
        if not edges.size:
            probe = load_stl_bytes(export_stl_bytes(fused))
            edges = _non_manifold_edges(probe)
        new_welds = _weld_boxes(probe, edges)
        if not new_welds:
            raise CityUnionError(
                "融合结果在 STL 回读后仍有非流形边",
                detail="无法定位需要修补的接触边",
            )
        welds.extend(new_welds)
    else:
        raise CityUnionError(
            "融合结果在 STL 回读后仍不水密",
            detail=f"经过 {MAX_WELD_PASSES} 次接触边修补仍未收敛",
        )

    assert fused is not None
    assert stl_bytes is not None and stl_mesh is not None
    bounds = np.asarray(base.bounds, dtype=float)
    return FusedCity(
        mesh=fused,
        base_bounds=(
            float(bounds[0, 0]),
            float(bounds[0, 1]),
            float(bounds[1, 0]),
            float(bounds[1, 1]),
        ),
        base_thickness_m=(
            float(base_thickness_m)
            if base_thickness_m is not None
            else float(base.extents[2])
        ),
        base_mesh=base,
        engine=MANIFOLD_ENGINE,
        weld_repairs=len(welds),
        stl_bytes=stl_bytes,
        stl_mesh=stl_mesh,
    )
