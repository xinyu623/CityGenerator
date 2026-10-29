from __future__ import annotations

import json
from pathlib import Path

from app.services.osm_parse import BuildingFeature, parse_buildings

FIXTURE = Path(__file__).parent / "fixtures" / "overpass_tiny.json"


def load_raw() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parse_buildings_extracts_ways_and_relation():
    features = parse_buildings(load_raw())
    assert len(features) == 3
    assert {feature.osm_id for feature in features} == {10, 11, 20}
    assert all(isinstance(feature, BuildingFeature) for feature in features)


def test_relation_is_assembled_with_hole():
    features = parse_buildings(load_raw())
    relation = next(feature for feature in features if feature.osm_id == 20)
    assert relation.osm_type == "relation"
    assert relation.geometry.geom_type == "Polygon"
    assert len(relation.geometry.interiors) == 1
    assert relation.geometry.area > 0


def test_invalid_and_tagless_ways_are_skipped():
    raw = load_raw()
    features = parse_buildings(raw)
    assert all(feature.geometry.geom_type in {"Polygon", "MultiPolygon"} for feature in features)
    assert all(feature.tags.get("building") for feature in features)
    assert 12 not in {feature.osm_id for feature in features}


def test_building_no_is_skipped():
    raw = load_raw()
    raw["elements"][-1]["tags"]["building"] = "no"
    features = parse_buildings(raw)
    assert 20 not in {feature.osm_id for feature in features}
