from __future__ import annotations

import pytest
import trimesh
from shapely.geometry import Polygon

from app.services.buildings import clean_polygons, detect_overlaps, extrude_building
from app.services.ground import build_ground
from app.services.stl_export import export_stl_bytes, load_stl_bytes


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
