from __future__ import annotations

import numpy as np
import trimesh
from trimesh.creation import box

from app.services.city import (
    COLLAPSE_TOLERANCE_M,
    FusedCity,
    fuse_city,
    separate_collapsed_vertices,
)
from app.services.errors import CityUnionError
from app.services.stl_export import (
    export_stl_bytes,
    load_stl_bytes,
    non_manifold_edge_count,
)


def _slab() -> trimesh.Trimesh:
    slab = box(extents=(40.0, 40.0, 1.0))
    slab.apply_translation((0.0, 0.0, -0.5))
    return slab


def test_fused_city_is_watertight_after_overlapping_buildings() -> None:
    first = box(extents=(10.0, 10.0, 20.0))
    first.apply_translation((0.0, 0.0, 9.5))
    second = box(extents=(10.0, 10.0, 20.0))
    second.apply_translation((5.0, 0.0, 9.5))

    fused = fuse_city([first, second], _slab())

    assert isinstance(fused, FusedCity)
    assert fused.mesh.is_watertight
    assert fused.mesh.is_winding_consistent
    assert fused.mesh.volume > 0.0
    assert non_manifold_edge_count(fused.mesh) == 0
    assert fused.engine == "manifold"


def test_fused_city_drops_internal_faces() -> None:
    outer = box(extents=(10.0, 10.0, 20.0))
    outer.apply_translation((0.0, 0.0, 9.5))
    inner = box(extents=(4.0, 4.0, 10.0))
    inner.apply_translation((0.0, 0.0, 4.5))
    slab = _slab()

    fused = fuse_city([outer, inner], slab)

    naive_triangles = len(outer.faces) + len(inner.faces) + len(slab.faces)
    assert len(fused.mesh.faces) < naive_triangles


def test_edge_touching_buildings_survive_stl_roundtrip() -> None:
    first = box(extents=(10.0, 10.0, 20.0))
    first.apply_translation((0.0, 0.0, 9.5))
    second = box(extents=(10.0, 10.0, 20.0))
    second.apply_translation((10.0, 10.0, 9.5))

    fused = fuse_city([first, second], _slab())
    loaded = load_stl_bytes(export_stl_bytes(fused.mesh))

    # Manifold closes edge contacts and sub-float32 vertex pairs are nudged
    # apart before the round-trip, so no weld boxes are needed here.
    assert fused.weld_repairs == 0
    assert loaded.is_watertight
    assert non_manifold_edge_count(loaded) == 0


def test_fused_city_rejects_open_building() -> None:
    closed = box(extents=(10.0, 10.0, 20.0))
    open_building = trimesh.Trimesh(
        vertices=closed.vertices, faces=closed.faces[:-1], process=False
    )

    try:
        fuse_city([open_building], _slab())
    except CityUnionError:
        pass
    else:
        raise AssertionError("an open building should not produce a closed city")


def test_separate_collapsed_vertices_moves_sub_tolerance_pairs_apart() -> None:
    mesh = trimesh.Trimesh(
        vertices=[
            [0.0, 0.0, 0.0],
            [0.0002, 0.0, 0.0],
            [0.0003, 0.0, 0.0],
            [1.0, 0.0, 0.0],
        ],
        faces=[[0, 1, 2], [1, 2, 3]],
        process=False,
    )

    moved = separate_collapsed_vertices(mesh)

    # The lowest index of a cluster stays put; the others move until every
    # remaining pair is at least the tolerance apart.
    assert moved == 3
    assert np.array_equal(mesh.vertices[0], [0.0, 0.0, 0.0])
    distances = np.linalg.norm(mesh.vertices[:, None] - mesh.vertices[None], axis=-1)
    np.fill_diagonal(distances, np.inf)
    assert distances.min() >= COLLAPSE_TOLERANCE_M


def test_separate_collapsed_vertices_finds_pairs_across_cell_boundaries() -> None:
    # 0.51 mm and 0.49 mm from the origin are 0.02 mm apart but fall into
    # different 0.5 mm buckets, and the higher bucket holds the lower index: the
    # scan must look in both directions, not only "forward" cells.
    mesh = trimesh.Trimesh(
        vertices=[[0.00051, 0.0, 0.0], [0.00049, 0.0, 0.0], [1.0, 0.0, 0.0]],
        faces=[[0, 1, 2]],
        process=False,
    )

    moved = separate_collapsed_vertices(mesh)

    assert moved == 1
    assert np.array_equal(mesh.vertices[0], [0.00051, 0.0, 0.0])
    assert np.linalg.norm(mesh.vertices[1] - mesh.vertices[0]) >= COLLAPSE_TOLERANCE_M
