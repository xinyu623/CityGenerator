from __future__ import annotations

import numpy as np
import pytest
import trimesh
from shapely.geometry import MultiPolygon, Polygon

from app.services.buildings import (
    PreparedBuilding,
    PreparedBuildings,
    ShellReport,
    _contact_indices,
    _known_height_pairs,
    _repair_shell,
    _shell_report,
    build_meshes,
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


def _triangle_signature(mesh: trimesh.Trimesh) -> list[tuple[float, ...]]:
    """Triangles as unordered vertex triples, rounded and sorted."""

    triangles = np.round(np.sort(np.asarray(mesh.triangles), axis=1), 9)
    return sorted(tuple(row.ravel()) for row in triangles)


def test_extruded_shell_matches_trimesh_polygon_construction():
    # The leaner shell builder must produce exactly the triangles trimesh's
    # extrude_polygon produces: same caps, same wall split, same winding.
    with_hole = Polygon(
        [(0, 0), (20, 0), (20, 20), (0, 20)],
        [[(8, 8), (12, 8), (12, 12), (8, 12)]],
    )
    l_shape = Polygon([(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)])
    clockwise = Polygon([(0, 0), (0, 10), (10, 10), (10, 0)])
    collinear = Polygon([(0, 0), (5, 0), (10, 0), (10, 10), (0, 10)])
    repeated = Polygon([(0, 0), (0, 0), (10, 0), (10, 10), (0, 10)])
    buffered = square().buffer(0.05, join_style=2, mitre_limit=2.0)
    for shape in (square(), l_shape, with_hole, clockwise, collinear, repeated, buffered):
        mesh = extrude_building(shape, height_m=12.0, base_m=0.0, embed_depth_m=0.0)
        reference = trimesh.creation.extrude_polygon(shape, 12.0)
        assert _triangle_signature(mesh) == _triangle_signature(reference)
        assert mesh.volume == pytest.approx(reference.volume)


def test_shell_report_accepts_a_closed_box():
    report = _shell_report(trimesh.creation.box(extents=(2.0, 2.0, 2.0)))
    assert report.watertight is True
    assert report.winding_consistent is True
    assert report.finite is True
    assert report.degenerate_faces == 0
    assert report.volume == pytest.approx(8.0)
    assert report.valid is True
    assert report.needs_repair is False


def test_shell_report_is_invalid_when_a_face_is_degenerate():
    report = ShellReport(
        watertight=True,
        winding_consistent=True,
        volume=1.0,
        degenerate_faces=1,
        finite=True,
    )
    assert report.needs_repair is True
    assert report.valid is False


def test_repair_shell_fixes_an_inverted_box():
    mesh = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
    mesh.invert()
    assert _shell_report(mesh).volume < 0.0

    _repair_shell(mesh)

    report = _shell_report(mesh)
    assert report.valid is True
    assert report.volume == pytest.approx(8.0)


def test_shell_report_rejects_an_open_shell():
    closed = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
    open_mesh = trimesh.Trimesh(vertices=closed.vertices, faces=closed.faces[:-1], process=False)
    report = _shell_report(open_mesh)
    assert report.watertight is False
    assert report.valid is False


def test_shell_report_rejects_a_non_manifold_edge():
    # Three triangles share edge (0, 1): every edge appears an even number of
    # times, so a pairs-only check would wrongly call it watertight.
    mesh = trimesh.Trimesh(
        vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1]],
        faces=[[0, 1, 2], [0, 1, 3], [0, 1, 4]],
        process=False,
    )
    report = _shell_report(mesh)
    assert report.watertight is False
    assert report.valid is False


def test_shell_report_rejects_an_inverted_shell():
    mesh = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
    mesh.invert()
    report = _shell_report(mesh)
    assert report.watertight is True
    assert report.winding_consistent is True
    assert report.volume < 0.0
    assert report.valid is False


def test_shell_report_counts_a_zero_area_face():
    closed = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
    vertices = np.vstack(
        [closed.vertices, [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]]
    )
    faces = np.vstack([closed.faces, [[8, 9, 10]]])
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    report = _shell_report(mesh)
    assert report.degenerate_faces == 1
    assert report.valid is False


def test_contact_indices_finds_touching_and_near_touching_footprints():
    left = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    right = Polygon([(10, 0), (20, 0), (20, 10), (10, 10)])
    near = Polygon([(20.04, 0), (30, 0), (30, 10), (20.04, 10)])
    far = Polygon([(40, 0), (50, 0), (50, 10), (40, 10)])

    assert _contact_indices([left, right, near, far], 0.1) == {0, 1, 2}
    assert _contact_indices([left, right, near, far], 0.0) == set()


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


def test_clamped_heights_are_not_sampling_observations():
    # height=900 becomes 300 with clamped=True: a censored OSM value, not an
    # observation to copy onto untagged buildings.
    features = [
        _feature(1, {"building": "yes", "height": "900"}, 0.000),
        _feature(2, {"building": "yes", "height": "12"}, 0.002),
    ]

    pairs = _known_height_pairs(features, 25.0, 3.0)

    assert [pair.height_m for pair in pairs] == [pytest.approx(12.0)]


def test_sampled_heights_respect_low_rise_type_caps():
    projector = projector_for_bbox(28.20, 112.90, 28.21, 112.91)
    features = [
        _feature(1, {"building": "tower", "height": "300"}, 0.000, size=0.0004),
        _feature(2, {"building": "train_station"}, 0.004, size=0.006),
    ]

    prepared = prepare_buildings(
        features,
        projector,
        min_height_samples=1,
        height_seed=1,
    )

    station = next(item for item in prepared.items if item.osm_id == 2)
    assert station.height_source == "sampled"
    assert station.height_m <= 60.0
    assert station.height_rule == "type_cap"
    assert prepared.height_sampling["type_capped"] == 1
    assert prepared.height_sampling["rules"]["type_cap"] == 1


def test_sampled_buildings_record_the_rule_that_produced_their_height():
    projector = projector_for_bbox(28.20, 112.90, 28.21, 112.91)
    features = [
        _feature(1, {"building": "house", "height": "6"}, 0.000),
        _feature(2, {"building": "house"}, 0.002),
    ]

    prepared = prepare_buildings(features, projector, min_height_samples=1, height_seed=3)

    house = next(item for item in prepared.items if item.osm_id == 2)
    assert house.height_source == "sampled"
    assert house.height_rule == "similarity"
    sampling = prepared.height_sampling
    assert sampling["rules"]["similarity"] == 1
    assert sampling["sampled_buildings"] == 1
    assert sampling["band_fallback_draws"] == 0
    assert sampling["weighting"] == "type_and_area_similarity"
    assert sampling["type_factors"] == {"same_type": 2.0, "type_group": 1.5}
    assert sampling["area_ratio"] == [0.5, 2.0]
    assert sampling["sampled_above_100m"] == 0
    assert sampling["pool_above_100m"] == 0
    assert sampling["above_100m_by_group"] == {"residential": 0}


def test_build_meshes_forwards_the_conservative_sampling_settings():
    projector = projector_for_bbox(28.20, 112.90, 28.21, 112.91)
    features = [
        _feature(1, {"building": "yes", "height": "250"}, 0.000),
        _feature(2, {"building": "yes"}, 0.002, size=0.0001),
    ]

    built = build_meshes(
        features,
        projector,
        min_height_samples=1,
        height_seed=5,
        height_conservative=True,
        height_slenderness_k=8.0,
    )

    assert built.height_sampling["conservative"] is True
    assert any(record.height_rule == "conservative_cap" for record in built.records)


def test_multipart_buildings_count_once_in_the_sampling_tail():
    projector = projector_for_bbox(28.20, 112.90, 28.21, 112.91)
    part_a = Polygon(
        [(112.9020, 28.2020), (112.9025, 28.2020), (112.9025, 28.2025), (112.9020, 28.2025)]
    )
    part_b = Polygon(
        [(112.9030, 28.2020), (112.9035, 28.2020), (112.9035, 28.2025), (112.9030, 28.2025)]
    )
    multipart = BuildingFeature(
        osm_type="relation",
        osm_id=10,
        tags={"building": "yes"},
        geometry=MultiPolygon([part_a, part_b]),
    )
    features = [_feature(1, {"building": "yes", "height": "9"}, 0.000), multipart]

    prepared = prepare_buildings(features, projector, min_height_samples=1, height_seed=0)

    assert len([item for item in prepared.items if item.height_source == "sampled"]) == 2
    assert prepared.height_sampling["sampled"] == 2
    assert prepared.height_sampling["sampled_buildings"] == 1


def test_conservative_sampling_is_opt_in():
    projector = projector_for_bbox(28.20, 112.90, 28.21, 112.91)
    features = [
        _feature(1, {"building": "yes", "height": "250"}, 0.000),
        _feature(2, {"building": "yes"}, 0.002, size=0.0001),
    ]

    loose = prepare_buildings(features, projector, min_height_samples=1, height_seed=5)
    tight = prepare_buildings(
        features,
        projector,
        min_height_samples=1,
        height_seed=5,
        height_conservative=True,
        height_slenderness_k=8.0,
    )

    tight_building = next(item for item in tight.items if item.osm_id == 2)
    assert tight_building.height_rule == "conservative_cap"
    assert tight_building.height_m < 250.0
    assert tight.height_sampling["conservative"] is True
    assert tight.height_sampling["slenderness_k"] == pytest.approx(8.0)
    assert loose.height_sampling["conservative"] is False
    assert loose.height_sampling["pool_above_250m"] == 1
    assert loose.height_sampling["sampled_above_250m"] == 1


def test_tagged_low_rise_height_is_not_capped():
    projector = projector_for_bbox(28.20, 112.90, 28.21, 112.91)
    features = [
        _feature(1, {"building": "train_station", "height": "100"}, 0.000, size=0.006),
        _feature(2, {"building": "yes", "height": "12"}, 0.004, size=0.0004),
    ]

    prepared = prepare_buildings(
        features,
        projector,
        min_height_samples=1,
        height_seed=3,
    )

    station = next(item for item in prepared.items if item.osm_id == 1)
    assert station.height_source == "tag"
    assert station.height_m == pytest.approx(100.0)


def test_height_sampling_metadata_has_one_shape_in_every_mode():
    # Sampling can run, be unavailable (too few tagged heights), or be switched
    # off. Consumers branch on ``enabled``/``reason``, never on which keys exist.
    projector = projector_for_bbox(28.20, 112.90, 28.21, 112.91)
    features = [
        _feature(1, {"building": "yes", "height": "12"}, 0.000),
        _feature(2, {"building": "yes"}, 0.002),
    ]

    enabled = prepare_buildings(features, projector, min_height_samples=1, height_seed=7)
    too_few = prepare_buildings(features, projector, min_height_samples=5, height_seed=7)
    disabled = prepare_buildings(features, projector, sample_missing_heights=False)

    shapes = {
        tuple(sorted(block.height_sampling))
        for block in (enabled, too_few, disabled)
    }
    assert len(shapes) == 1
    assert shapes.pop() == tuple(
        sorted(
            {
                "enabled",
                "reason",
                "known",
                "jitter",
                "seed",
                "weighting",
                "type_factors",
                "area_bandwidth_log",
                "area_ratio",
                "area_weighted",
                "conservative",
                "slenderness_k",
                "pool_above_100m",
                "pool_above_250m",
                "sampled",
                "sampled_buildings",
                "rules",
                "type_capped",
                "band_fallback_draws",
                "sampled_above_100m",
                "sampled_above_250m",
                "above_100m_by_group",
            }
        )
    )

    running = enabled.height_sampling
    assert running["enabled"] is True
    assert running["reason"] == ""
    assert running["sampled"] == 1
    assert running["weighting"] == "type_and_area_similarity"
    assert running["area_weighted"] is True

    sparse = too_few.height_sampling
    assert sparse["enabled"] is False
    assert sparse["reason"] == "too_few_known_heights"
    assert sparse["known"] == 1

    off = disabled.height_sampling
    assert off["enabled"] is False
    assert off["reason"] == "disabled"
    assert off["known"] == 1
    for block in (sparse, off):
        assert block["sampled"] == 0
        assert block["sampled_buildings"] == 0
        assert block["type_capped"] == 0
        assert block["band_fallback_draws"] == 0
        assert block["sampled_above_100m"] == 0
        assert block["sampled_above_250m"] == 0
        assert block["rules"] == {}
        assert block["above_100m_by_group"] == {}
        assert block["area_weighted"] is False
        assert block["weighting"] == "none"
        assert block["area_ratio"] == pytest.approx([0.5, 2.0])
        assert block["type_factors"] == {"same_type": 2.0, "type_group": 1.5}
        assert block["pool_above_100m"] == 0
        assert block["pool_above_250m"] == 0
