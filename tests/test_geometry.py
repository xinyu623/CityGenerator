from __future__ import annotations

import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon

from app.services.buildings import (
    PreparedBuilding,
    PreparedBuildings,
    clean_polygons,
    detect_overlaps,
    extrude_building,
    extrude_prepared,
    prepare_buildings,
)
from app.services.city import fuse_city, separate_collapsed_vertices
from app.services.ground import build_ground
from app.services.osm_parse import BuildingFeature
from app.services.projection import projector_for_bbox
from app.services.stl_export import (
    export_stl_bytes,
    load_stl_bytes,
    non_manifold_edge_count,
)


def square(size: float = 10.0) -> Polygon:
    return Polygon([(0, 0), (size, 0), (size, size), (0, size)])


def test_clean_polygon_keeps_simple_square():
    result = clean_polygons(square())
    assert len(result) == 1
    assert result[0].is_valid
    assert result[0].area == pytest.approx(100.0)


def test_clean_polygon_repairs_bowtie():
    bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
    result = clean_polygons(bowtie)
    assert result
    assert all(poly.is_valid for poly in result)
    assert sum(poly.area for poly in result) > 0


def test_clean_polygon_drops_tiny_slivers():
    tiny = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    assert clean_polygons(tiny, min_area_m2=4.0) == []


def test_clean_polygon_preserves_hole():
    outer = [(0, 0), (20, 0), (20, 20), (0, 20)]
    hole = [(8, 8), (12, 8), (12, 12), (8, 12)]
    polygon = Polygon(outer, [hole])
    result = clean_polygons(polygon)
    assert len(result) == 1
    assert len(result[0].interiors) == 1
    assert result[0].area == pytest.approx(400.0 - 16.0)


def test_extrude_square_has_expected_bounds_and_volume():
    mesh = extrude_building(square(), height_m=12.0, base_m=0.0, embed_depth_m=0.5)
    assert mesh.is_watertight
    assert mesh.is_winding_consistent
    assert mesh.bounds[0][2] == pytest.approx(-0.5)
    assert mesh.bounds[1][2] == pytest.approx(12.0)
    assert mesh.volume == pytest.approx(10.0 * 10.0 * 12.5)


def test_extrude_with_base_does_not_embed():
    mesh = extrude_building(square(), height_m=12.0, base_m=3.0, embed_depth_m=0.5)
    assert mesh.bounds[0][2] == pytest.approx(3.0)
    assert mesh.bounds[1][2] == pytest.approx(12.0)
    assert mesh.volume == pytest.approx(10.0 * 10.0 * 9.0)


def test_extrude_polygon_with_hole_is_watertight():
    polygon = Polygon(
        [(0, 0), (20, 0), (20, 20), (0, 20)],
        [[(8, 8), (12, 8), (12, 12), (8, 12)]],
    )
    mesh = extrude_building(polygon, height_m=10.0)
    assert mesh.is_watertight
    assert mesh.volume == pytest.approx((400.0 - 16.0) * 10.5)


def test_detect_overlaps_counts_partial_overlap():
    left = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    right = Polygon([(5, 0), (15, 0), (15, 10), (5, 10)])
    assert detect_overlaps([left, right]) == 1


def test_detect_overlaps_ignores_touching_footprints():
    left = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    right = Polygon([(10, 0), (20, 0), (20, 10), (10, 10)])
    assert detect_overlaps([left, right]) == 0


def test_ground_plane_area_and_normals():
    ground = build_ground((0.0, 0.0, 100.0, 50.0), mode="plane")
    assert ground.area == pytest.approx(5000.0)
    assert ground.is_watertight is False
    assert all(abs(normal[2] - 1.0) < 1e-9 for normal in ground.face_normals)


def test_ground_slab_is_watertight():
    ground = build_ground((0.0, 0.0, 100.0, 50.0), mode="slab", thickness_m=1.0)
    assert ground.is_watertight
    assert ground.volume == pytest.approx(5000.0)


def test_stl_export_round_trip():
    mesh = extrude_building(square(), height_m=12.0)
    data = export_stl_bytes(mesh)
    assert isinstance(data, bytes)
    assert len(data) == 84 + 50 * len(mesh.faces)

    loaded = load_stl_bytes(data)
    assert len(loaded.faces) == len(mesh.faces)
    assert loaded.is_watertight


def test_binary_stl_header_has_triangle_count():
    mesh = trimesh.creation.box(extents=(1, 1, 1))
    data = export_stl_bytes(mesh)
    count = int.from_bytes(data[80:84], "little")
    assert count == len(mesh.faces)


def test_non_manifold_edge_count_is_zero_for_closed_box():
    mesh = trimesh.creation.box(extents=(2, 2, 2))
    assert non_manifold_edge_count(mesh) == 0


def test_non_manifold_edge_count_detects_touching_shells():
    left = trimesh.creation.box(extents=(10, 10, 10))
    right = trimesh.creation.box(extents=(10, 10, 10))
    right.apply_translation((10, 0, 0))
    combined = trimesh.util.concatenate([left, right])
    combined.merge_vertices()
    assert non_manifold_edge_count(combined) > 0


def test_non_manifold_edge_count_counts_an_edge_shared_by_three_faces_once():
    # Three triangles share the edge (0, 1): exactly one non-manifold edge.
    mesh = trimesh.Trimesh(
        vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1]],
        faces=[[0, 1, 2], [0, 1, 3], [0, 1, 4]],
        process=False,
    )
    assert non_manifold_edge_count(mesh) == 1


def _prepared(*polygons: Polygon, height: float = 12.0) -> PreparedBuildings:
    return PreparedBuildings(
        items=[
            PreparedBuilding(
                osm_type="way",
                osm_id=index + 1,
                tags={"building": "yes"},
                source_geometry=polygon,
                polygon_local=polygon,
                height_m=height,
                base_m=0.0,
                height_source="tag",
                area_m2=polygon.area,
            )
            for index, polygon in enumerate(polygons)
        ]
    )


def test_corner_touching_buildings_fuse_without_a_weld():
    # The two shells meet along one vertical edge: no common interior, so the
    # raw union is not manifold. A 0.1 m contact overlap gives them real shared
    # volume and the weld repair pass is no longer needed.
    left = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    right = Polygon([(10, 10), (20, 10), (20, 20), (10, 20)])
    built = extrude_prepared(_prepared(left, right), terrain=None, contact_overlap_m=0.1)
    city = fuse_city(built.meshes, build_ground((0, 0, 20, 20), mode="slab", thickness_m=1.0))

    assert city.mesh.is_watertight
    assert city.weld_repairs == 0


def test_corner_touching_buildings_fuse_without_welds():
    # Manifold returns a closed union for edge/corner contacts and the STL
    # round-trip no longer collapses their near-coincident vertices, so neither
    # the contact overlap nor a weld repair box is required to close the city.
    left = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    right = Polygon([(10, 10), (20, 10), (20, 20), (10, 20)])
    built = extrude_prepared(_prepared(left, right), terrain=None, contact_overlap_m=0.0)
    city = fuse_city(built.meshes, build_ground((0, 0, 20, 20), mode="slab", thickness_m=1.0))

    assert city.mesh.is_watertight
    assert city.weld_repairs == 0


def test_isolated_buildings_are_not_grown_by_the_contact_overlap():
    near = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    far = Polygon([(50, 0), (60, 0), (60, 10), (50, 10)])
    built = extrude_prepared(_prepared(near, far), terrain=None, contact_overlap_m=0.1)

    assert built.meshes[0].volume == pytest.approx(10.0 * 10.0 * 12.5)
    assert built.meshes[1].volume == pytest.approx(10.0 * 10.0 * 12.5)


def test_separate_collapsed_vertices_moves_sub_millimetre_pairs():
    mesh = trimesh.Trimesh(
        vertices=[
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0002, 0.0, 0.0],
            [2.0, 0.0, 0.0],
            [2.0, 1.0, 0.0],
        ],
        faces=[[0, 1, 2], [3, 4, 5]],
        process=False,
    )
    moved = separate_collapsed_vertices(mesh)

    assert moved == 1
    assert float(np.linalg.norm(mesh.vertices[3] - mesh.vertices[0])) >= 1e-3


def _feature(
    osm_id: int,
    tags: dict[str, str],
    offset: float = 0.0,
    size: float = 0.0005,
) -> BuildingFeature:
    ring = [
        (112.9000 + offset, 28.2000),
        (112.9000 + offset + size, 28.2000),
        (112.9000 + offset + size, 28.2000 + size),
        (112.9000 + offset, 28.2000 + size),
    ]
    return BuildingFeature(
        osm_type="way",
        osm_id=osm_id,
        tags=tags,
        geometry=Polygon(ring),
    )


def test_missing_heights_are_sampled_from_the_area_distribution():
    projector = projector_for_bbox(28.20, 112.90, 28.21, 112.91)
    features = [
        _feature(1, {"building": "yes", "height": "12"}, 0.000),
        _feature(2, {"building": "yes", "building:levels": "20"}, 0.002),
        _feature(3, {"building": "yes"}, 0.004),
        _feature(4, {"building": "yes"}, 0.006),
        _feature(5, {"building": "yes"}, 0.008),
    ]

    prepared = prepare_buildings(
        features,
        projector,
        min_height_samples=2,
        height_seed=7,
    )

    heights = {item.osm_id: item.height_m for item in prepared.items}
    sources = {item.osm_id: item.height_source for item in prepared.items}
    assert sources[1] == "tag" and heights[1] == pytest.approx(12.0)
    assert sources[2] == "levels" and heights[2] == pytest.approx(60.0)
    for osm_id in (3, 4, 5):
        assert sources[osm_id] == "sampled"
        assert heights[osm_id] != pytest.approx(25.0)
        assert 9.0 * 0.9 - 1e-9 <= heights[osm_id] <= 60.0 * 1.1 + 1e-9
    assert prepared.height_sampling["known"] == 2
    assert prepared.height_sampling["sampled"] == 3


def test_sampled_heights_follow_footprint_size():
    projector = projector_for_bbox(28.20, 112.90, 28.21, 112.91)
    features = [
        _feature(1, {"building": "yes", "height": "4"}, 0.000, size=0.0004),
        _feature(2, {"building": "yes", "height": "100"}, 0.002, size=0.006),
        _feature(3, {"building": "yes"}, 0.010, size=0.0004),
        _feature(4, {"building": "yes"}, 0.012, size=0.006),
    ]

    prepared = prepare_buildings(
        features,
        projector,
        min_height_samples=2,
        height_seed=7,
    )

    heights = {item.osm_id: item.height_m for item in prepared.items}
    # The small shed draws from the small tagged building, the large footprint
    # from the tower; a uniform pool would mix 4 m and 100 m for both.
    assert heights[3] < 20.0
    assert heights[4] > 80.0
    assert prepared.height_sampling["area_weighted"] is True


def test_missing_heights_fall_back_to_the_default_without_enough_samples():
    projector = projector_for_bbox(28.20, 112.90, 28.21, 112.91)
    features = [
        _feature(1, {"building": "yes", "height": "12"}, 0.000),
        _feature(2, {"building": "yes"}, 0.002),
        _feature(3, {"building": "yes"}, 0.004),
    ]

    prepared = prepare_buildings(
        features,
        projector,
        min_height_samples=5,
        height_seed=7,
    )

    assert all(item.height_source == "default" for item in prepared.items if item.osm_id > 1)
    assert all(item.height_m == pytest.approx(25.0) for item in prepared.items if item.osm_id > 1)
    assert prepared.height_sampling["enabled"] is False
