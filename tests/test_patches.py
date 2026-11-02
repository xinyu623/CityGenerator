from __future__ import annotations

import numpy as np
import trimesh
from trimesh.creation import box

from app.services.city import fuse_city
from app.services.patches import BOTTOM, GROUND, WALL, face_kinds, patch_table
from app.services.stl_export import export_stl_bytes, load_stl_bytes
from app.services.terrain import TerrainGrid, build_terrain_solid


def counts_of(kinds: np.ndarray) -> dict[str, int]:
    return {
        "ground": int((kinds == GROUND).sum()),
        "wall": int((kinds == WALL).sum()),
        "bottom": int((kinds == BOTTOM).sum()),
    }


def test_flat_slab_is_split_into_ground_wall_bottom():
    slab = box(extents=(10.0, 10.0, 1.0))
    slab.apply_translation((0.0, 0.0, -0.5))

    kinds = face_kinds(slab)

    assert counts_of(kinds) == {"ground": 2, "wall": 8, "bottom": 2}


def test_terrain_top_is_ground_even_on_a_slope():
    grid = TerrainGrid(
        heights=np.array([[0.0, 2.0], [0.0, 2.0]]),
        x0=0.0,
        y0=0.0,
        dx=10.0,
        dy=10.0,
        elevation_min_m=0.0,
        elevation_max_m=2.0,
    )
    solid = build_terrain_solid(grid, thickness_m=1.0)

    kinds = face_kinds(solid, terrain=grid)

    counts = counts_of(kinds)
    assert counts["ground"] == 2
    assert counts["bottom"] > 0
    assert counts["wall"] > 0
    centers = solid.triangles_center[kinds == GROUND]
    assert np.allclose(centers[:, 2], grid.surface_z(centers[:, 0], centers[:, 1]), atol=1e-4)


def test_roof_is_wall_not_ground():
    slab = box(extents=(30.0, 30.0, 1.0))
    slab.apply_translation((0.0, 0.0, -0.5))
    building = box(extents=(10.0, 10.0, 20.0))
    building.apply_translation((0.0, 0.0, 9.0))  # sits 1 m into the slab
    fused = fuse_city([building], slab)

    kinds = face_kinds(fused.mesh)

    counts = counts_of(kinds)
    assert counts["bottom"] == 2  # only the slab underside
    assert counts["ground"] > 0
    assert counts["wall"] > 0
    centers = fused.mesh.triangles_center
    assert np.all(centers[kinds == GROUND][:, 2] <= 0.01)
    # The roof is upward-facing but high above the ground, so it is a wall.
    upward = fused.mesh.face_normals[:, 2] > 1e-6
    roof = upward & (kinds != GROUND)
    assert roof.any()
    assert np.all(centers[roof][:, 2] > 1.0)


def test_near_vertical_wall_is_not_misclassified_as_bottom():
    # STL float32 noise leaves a short vertical wall's normal with nz ~ -1e-3;
    # it must stay a wall instead of becoming a second "bottom".
    mesh = trimesh.Trimesh(
        vertices=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, -1.1e-3, 1.0]],
        faces=[[0, 1, 2]],
        process=False,
    )
    assert mesh.face_normals[0][2] < -1e-3

    kinds = face_kinds(mesh)

    assert kinds[0] == WALL


def test_patch_table_ranges_cover_every_triangle_without_overlap():
    mesh = box(extents=(10.0, 10.0, 1.0))
    mesh.apply_translation((0.0, 0.0, -0.5))
    kinds = face_kinds(mesh)

    table = patch_table(kinds, ground_tolerance_m=0.05)

    assert table["triangle_count"] == len(kinds)
    assert sum(table["counts"].values()) == len(kinds)
    expected_start = 0
    for item in table["ranges"]:
        assert set(item) == {"start", "count", "kind"}
        assert item["start"] == expected_start
        assert item["count"] > 0
        expected_start += item["count"]
    assert expected_start == len(kinds)
    for kind in ("ground", "wall", "bottom"):
        assert table["counts"][kind] == sum(
            item["count"] for item in table["ranges"] if item["kind"] == kind
        )


def test_patch_ranges_follow_the_stl_triangle_order():
    slab = box(extents=(30.0, 30.0, 1.0))
    slab.apply_translation((0.0, 0.0, -0.5))
    building = box(extents=(10.0, 10.0, 20.0))
    building.apply_translation((0.0, 0.0, 20.0))
    fused = fuse_city([building], slab)

    loaded = load_stl_bytes(export_stl_bytes(fused.mesh))
    kinds = face_kinds(loaded)
    table = patch_table(kinds, ground_tolerance_m=0.05)

    normals = loaded.face_normals
    for item in table["ranges"]:
        block = normals[item["start"] : item["start"] + item["count"]]
        if item["kind"] == "bottom":
            assert np.all(block[:, 2] < 0.0)
        elif item["kind"] == "ground":
            assert np.all(block[:, 2] > 0.0)
    assert table["triangle_count"] == len(loaded.faces)
