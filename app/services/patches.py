"""CFD patch groups: classify fused triangles as ground, wall, or bottom.

``city.stl`` is one closed surface, but a solver still has to tell the ground
from the building walls. This module classifies every triangle and builds
``patches.json``: contiguous triangle ranges in STL order, so a solver can
assign boundary patches by walking the file once.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import trimesh

from app.services.terrain import TerrainGrid

WALL = 0
GROUND = 1
BOTTOM = 2
KIND_NAMES = ("wall", "ground", "bottom")
SCHEMA_VERSION = "1.0"
# STL vertices are float32 and the union re-triangulates, so a short vertical
# wall triangle can get |nz| ~ 1e-3. Ground detection therefore needs a real
# upward slope (0.6 deg) and the bottom is recognised geometrically (every
# vertex on the base underside), not from the sign of nz alone.
_GROUND_MIN_NZ = 0.01


def face_kinds(
    mesh: trimesh.Trimesh,
    *,
    terrain: TerrainGrid | None = None,
    ground_tolerance_m: float = 0.05,
) -> np.ndarray:
    """Classify every face, in mesh face order.

    ``bottom`` is the base underside: every vertex sits on the mesh's lowest
    plane. Among the remaining upward faces, ``ground`` means the face centroid
    is within ``ground_tolerance_m`` of the terrain surface (the flat ground at
    ``z = 0`` when terrain is disabled); every other upward face is a roof and
    stays ``wall``. Vertical faces are walls, including the terrain skirt and
    the float32-noise tilt on short wall triangles.
    """

    normals = np.asarray(mesh.face_normals, dtype=float)
    nz = normals[:, 2]
    kinds = np.full(len(nz), WALL, dtype=np.int8)

    if len(nz):
        triangles = np.asarray(mesh.triangles, dtype=float)
        bottom_z = float(np.asarray(mesh.vertices)[:, 2].min())
        on_bottom = (np.abs(triangles[:, :, 2] - bottom_z) <= ground_tolerance_m).all(axis=1)
        kinds[on_bottom] = BOTTOM

        upward = np.nonzero((nz > _GROUND_MIN_NZ) & ~on_bottom)[0]
        if upward.size:
            centers = np.asarray(mesh.triangles_center, dtype=float)[upward]
            if terrain is not None:
                surface = np.asarray(
                    terrain.surface_z(centers[:, 0], centers[:, 1]), dtype=float
                )
            else:
                surface = np.zeros(len(centers), dtype=float)
            on_surface = np.abs(centers[:, 2] - surface) <= ground_tolerance_m
            kinds[upward[on_surface]] = GROUND
    return kinds


def patch_table(kinds: np.ndarray, *, ground_tolerance_m: float) -> dict[str, Any]:
    """Build the ``patches.json`` payload: counts plus contiguous ranges."""

    ranges: list[dict[str, Any]] = []
    start = 0
    for index in range(1, len(kinds) + 1):
        if index == len(kinds) or kinds[index] != kinds[start]:
            ranges.append(
                {
                    "start": start,
                    "count": index - start,
                    "kind": KIND_NAMES[int(kinds[start])],
                }
            )
            start = index

    counts = {name: 0 for name in KIND_NAMES}
    for item in ranges:
        counts[item["kind"]] += item["count"]

    return {
        "schema_version": SCHEMA_VERSION,
        "unit": "m",
        "triangle_count": int(len(kinds)),
        "ground_tolerance_m": ground_tolerance_m,
        "counts": counts,
        "ranges": ranges,
    }
