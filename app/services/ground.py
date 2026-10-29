"""Ground surface generation."""

from __future__ import annotations

import trimesh


def build_ground(
    bounds: tuple[float, float, float, float],
    mode: str = "plane",
    thickness_m: float = 1.0,
) -> trimesh.Trimesh:
    """Build a ground plane at z=0 or a closed slab from z=-thickness to z=0."""

    minx, miny, maxx, maxy = (float(value) for value in bounds)
    if maxx <= minx or maxy <= miny:
        raise ValueError("ground bounds must have positive width and height")

    if mode == "plane":
        vertices = [
            (minx, miny, 0.0),
            (maxx, miny, 0.0),
            (maxx, maxy, 0.0),
            (minx, maxy, 0.0),
        ]
        faces = [(0, 1, 2), (0, 2, 3)]
        return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    if mode == "slab":
        if thickness_m <= 0.0:
            raise ValueError("slab thickness must be positive")
        mesh = trimesh.creation.box(extents=(maxx - minx, maxy - miny, float(thickness_m)))
        mesh.apply_translation(
            (
                (minx + maxx) / 2.0,
                (miny + maxy) / 2.0,
                -float(thickness_m) / 2.0,
            )
        )
        return mesh

    raise ValueError(f"unknown ground mode: {mode}")
