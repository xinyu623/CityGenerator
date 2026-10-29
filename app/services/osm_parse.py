"""Convert Overpass JSON into building features."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import osm2geojson
from shapely.geometry import MultiPolygon, Polygon

from app.services.errors import OsmParseError

_SKIP_BUILDING_VALUES = {"no", "construction", "proposed"}


@dataclass
class BuildingFeature:
    osm_type: str
    osm_id: int
    tags: dict[str, str]
    geometry: Polygon | MultiPolygon


def _as_polygon(geometry: object) -> Polygon | MultiPolygon | None:
    if isinstance(geometry, Polygon):
        return geometry
    if isinstance(geometry, MultiPolygon):
        if len(geometry.geoms) == 1:
            return geometry.geoms[0]
        return geometry
    geoms = getattr(geometry, "geoms", None)
    if geoms is not None:
        polygons = [part for part in geoms if isinstance(part, Polygon)]
        if len(polygons) == 1:
            return polygons[0]
        if polygons:
            return MultiPolygon(polygons)
    return None


def _relation_member_way_ids(raw: dict[str, Any]) -> set[int]:
    used: set[int] = set()
    for element in raw.get("elements", []):
        if element.get("type") != "relation":
            continue
        tags = element.get("tags") or {}
        if not tags.get("building"):
            continue
        for member in element.get("members", []):
            if member.get("type") == "way" and member.get("ref") is not None:
                used.add(int(member["ref"]))
    return used


def parse_buildings(raw: dict[str, Any]) -> list[BuildingFeature]:
    """Extract valid building polygons from an Overpass JSON response.

    ``osm2geojson`` is asked to return every element so we can filter ways
    ourselves. Ways that are members of a building relation are skipped to
    avoid duplicate footprints.
    """

    if not isinstance(raw, dict):
        raise OsmParseError("OSM 数据格式无效")

    try:
        shapes = osm2geojson.json2shapes(raw, filter_used_refs=False)
    except Exception as exc:
        raise OsmParseError("OSM 数据解析失败", str(exc)) from exc

    used_way_ids = _relation_member_way_ids(raw)
    features: list[BuildingFeature] = []
    seen: set[tuple[str, int]] = set()

    for item in shapes:
        geometry = item.get("shape")
        props = item.get("properties") or {}
        tags_raw = props.get("tags") or {}
        if not isinstance(tags_raw, dict):
            continue

        building = tags_raw.get("building")
        if not building or str(building).lower() in _SKIP_BUILDING_VALUES:
            continue

        osm_type = str(props.get("type") or props.get("osm_type") or "")
        if osm_type not in {"way", "relation"}:
            continue
        try:
            osm_id = int(props.get("id") or props.get("osm_id") or 0)
        except (TypeError, ValueError):
            continue
        if osm_id <= 0:
            continue
        if osm_type == "way" and osm_id in used_way_ids:
            continue

        key = (osm_type, osm_id)
        if key in seen:
            continue

        polygon = _as_polygon(geometry)
        if polygon is None or polygon.is_empty:
            continue

        tags = {str(key): str(value) for key, value in tags_raw.items()}
        features.append(
            BuildingFeature(
                osm_type=osm_type,
                osm_id=osm_id,
                tags=tags,
                geometry=polygon,
            )
        )
        seen.add(key)

    return features
