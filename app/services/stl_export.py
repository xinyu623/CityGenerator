"""Binary STL export, loading, and validation."""

from __future__ import annotations

import io
from dataclasses import dataclass

import trimesh


@dataclass(frozen=True)
class MeshValidation:
    watertight: bool
    winding_consistent: bool
    volume: float
    triangles: int
    degenerate: int


def export_stl_bytes(mesh: trimesh.Trimesh) -> bytes:
    """Export a mesh as a binary STL byte string."""

    data = trimesh.exchange.stl.export_stl(mesh)
    if isinstance(data, str):
        data = data.encode("utf-8")
    return data


def load_stl_bytes(data: bytes) -> trimesh.Trimesh:
    """Load a binary or ASCII STL from bytes."""

    loaded = trimesh.load(io.BytesIO(data), file_type="stl", process=True)
    if isinstance(loaded, trimesh.Scene):
        geometries = list(loaded.geometry.values())
        if not geometries:
            raise ValueError("STL contains no geometry")
        loaded = trimesh.util.concatenate(geometries)
    loaded.merge_vertices()
    if hasattr(loaded, "remove_unreferenced_vertices"):
        loaded.remove_unreferenced_vertices()
    return loaded


def validate_mesh(mesh: trimesh.Trimesh) -> MeshValidation:
    try:
        degenerate = int(len(mesh.faces) - len(mesh.nondegenerate_faces()))
    except Exception:
        degenerate = 0
    try:
        volume = float(mesh.volume)
    except Exception:
        volume = 0.0
    return MeshValidation(
        watertight=bool(mesh.is_watertight),
        winding_consistent=bool(mesh.is_winding_consistent),
        volume=volume,
        triangles=int(len(mesh.faces)),
        degenerate=degenerate,
    )


def binary_stl_triangle_count(data: bytes) -> int:
    if len(data) < 84:
        raise ValueError("STL data is too short")
    return int.from_bytes(data[80:84], "little")
