"""Fuse building shells and a solid base into one watertight city mesh."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import trimesh

from app.services.errors import CityUnionError
from app.services.stl_export import non_manifold_edge_count

MANIFOLD_ENGINE = "manifold"


@dataclass(frozen=True)
class FusedCity:
    mesh: trimesh.Trimesh
    base_bounds: tuple[float, float, float, float]
    base_thickness_m: float
    engine: str = MANIFOLD_ENGINE


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

    try:
        fused = trimesh.boolean.union([*buildings, base], engine=MANIFOLD_ENGINE)
    except Exception as exc:
        raise CityUnionError(
            "建筑与地面布尔融合失败",
            detail=str(exc),
        ) from exc

    if not isinstance(fused, trimesh.Trimesh):
        raise CityUnionError("布尔融合返回了非网格结果")

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
    )
