"""Fuse building shells and a solid base into one watertight city mesh."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import trimesh
from trimesh.creation import cylinder
from trimesh.geometry import align_vectors

from app.services.errors import CityUnionError
from app.services.stl_export import (
    export_stl_bytes,
    load_stl_bytes,
    non_manifold_edge_count,
)

MANIFOLD_ENGINE = "manifold"
MAX_WELD_PASSES = 6
WELD_RADIUS_M = 0.25
WELD_EXTRA_M = 0.8


@dataclass(frozen=True)
class FusedCity:
    mesh: trimesh.Trimesh
    base_bounds: tuple[float, float, float, float]
    base_thickness_m: float
    engine: str = MANIFOLD_ENGINE
    weld_repairs: int = 0


def _non_manifold_edges(mesh: trimesh.Trimesh) -> np.ndarray:
    edges = np.asarray(mesh.edges_sorted)
    if not edges.size:
        return np.empty((0, 2), dtype=np.int64)
    unique, counts = np.unique(edges, axis=0, return_counts=True)
    return unique[counts > 2]


def _weld_cylinders(mesh: trimesh.Trimesh, edges: np.ndarray) -> list[trimesh.Trimesh]:
    welds = []
    for edge in edges:
        start, end = mesh.vertices[edge[0]], mesh.vertices[edge[1]]
        direction = end - start
        length = float(np.linalg.norm(direction))
        if length < 1e-8:
            continue
        weld = cylinder(
            radius=WELD_RADIUS_M,
            height=length + WELD_EXTRA_M,
            sections=12,
        )
        weld.apply_transform(align_vectors([0.0, 0.0, 1.0], direction))
        weld.apply_translation((start + end) / 2.0)
        welds.append(weld)
    return welds


def _stl_roundtrip_is_closed(mesh: trimesh.Trimesh) -> bool:
    loaded = load_stl_bytes(export_stl_bytes(mesh))
    return bool(loaded.is_watertight and non_manifold_edge_count(loaded) == 0)


def validate_city(mesh: trimesh.Trimesh) -> None:
    """Raise ``CityUnionError`` unless the fused mesh is a closed solid."""

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


def fuse_city(
    buildings: Sequence[trimesh.Trimesh],
    base: trimesh.Trimesh,
) -> FusedCity:
    """Boolean-union every building shell with the solid base."""

    if not buildings:
        raise CityUnionError("没有可融合的建筑壳体")
    if base is None or not len(base.faces):
        raise CityUnionError("缺少实体地面")

    meshes: list[trimesh.Trimesh] = [*buildings, base]
    welds: list[trimesh.Trimesh] = []
    fused: trimesh.Trimesh | None = None

    for _ in range(MAX_WELD_PASSES):
        try:
            fused = trimesh.boolean.union([*meshes, *welds], engine=MANIFOLD_ENGINE)
        except Exception as exc:
            raise CityUnionError(
                "建筑与地面布尔融合失败",
                detail=str(exc),
            ) from exc

        if not isinstance(fused, trimesh.Trimesh):
            raise CityUnionError("布尔融合返回了非网格结果")
        validate_city(fused)

        if _stl_roundtrip_is_closed(fused):
            break

        probe = fused.copy()
        probe.merge_vertices()
        edges = _non_manifold_edges(probe)
        if not edges.size:
            probe = load_stl_bytes(export_stl_bytes(fused))
            edges = _non_manifold_edges(probe)
        new_welds = _weld_cylinders(probe, edges)
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
    validate_city(fused)
    bounds = np.asarray(base.bounds, dtype=float)
    return FusedCity(
        mesh=fused,
        base_bounds=(
            float(bounds[0, 0]),
            float(bounds[0, 1]),
            float(bounds[1, 0]),
            float(bounds[1, 1]),
        ),
        base_thickness_m=float(base.extents[2]),
        engine=MANIFOLD_ENGINE,
        weld_repairs=len(welds),
    )
