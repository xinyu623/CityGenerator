"""Write the fused city STL, metadata, GeoJSON, README, and download ZIP."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import trimesh
from shapely.geometry import mapping
from trimesh.creation import box

from app.config import Settings
from app.models.schemas import FileInfo, GenerateRequest, WarningItem
from app.services.buildings import BuildResult
from app.services.city import FusedCity, validate_city
from app.services.errors import CityUnionError
from app.services.io_utils import utc_now_iso, write_bytes_atomic, write_json_atomic
from app.services.projection import Projector
from app.services.stl_export import (
    export_stl_bytes,
    load_stl_bytes,
    non_manifold_edge_count,
)

SCHEMA_VERSION = "1.0"


@dataclass
class OutputBundle:
    files: list[FileInfo]
    zip_path: Path
    zip_size: int
    triangle_count: int
    metadata: dict[str, Any]


def _selected_tags(tags: dict[str, str]) -> dict[str, str]:
    keep = ("building", "name", "height", "building:levels", "min_height", "roof:height")
    return {key: value for key, value in tags.items() if key in keep}


def _build_metadata(
    *,
    job_id: str,
    request: GenerateRequest,
    projector: Projector,
    build_result: BuildResult,
    non_manifold_edges: int,
    overpass_meta: dict[str, Any],
    area_km2: float,
    city: FusedCity,
    settings: Settings,
    warnings: list[WarningItem],
) -> dict[str, Any]:
    origin_lon, origin_lat = projector.origin_wgs84()
    heights = [record.height_m for record in build_result.records]
    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": settings.app_name, "version": settings.version},
        "created_at": utc_now_iso(),
        "job_id": job_id,
        "unit": "m",
        "local_frame": {
            "x": "east",
            "y": "north",
            "z": "up",
            "unit": "m",
            "origin": {
                "easting": round(projector.origin_easting, 3),
                "northing": round(projector.origin_northing, 3),
                "lon": round(origin_lon, 9),
                "lat": round(origin_lat, 9),
            },
        },
        "crs_projected": projector.crs,
        "bbox_wgs84": request.bbox.model_dump(),
        "area_km2": round(area_km2, 6),
        "attribution": "© OpenStreetMap contributors",
        "license": "ODbL-1.0",
        "buildings": {
            "count": len(build_result.records),
            "triangle_count": sum(len(record.mesh.faces) for record in build_result.records),
            "height_sources": dict(build_result.height_sources),
            "skipped": build_result.skipped,
            "min_height_m": round(min(heights), 3) if heights else 0.0,
            "max_height_m": round(max(heights), 3) if heights else 0.0,
            "footprint_area_m2": round(sum(record.area_m2 for record in build_result.records), 3),
            "default_height_m": request.options.default_height_m,
            "level_height_m": request.options.level_height_m,
            "embed_depth_m": request.options.embed_depth_m,
            "each_shell_watertight": all(
                record.mesh.is_watertight for record in build_result.records
            ),
        },
        "ground": {
            "included": True,
            "mode": "slab",
            "z": 0.0,
            "thickness_m": city.base_thickness_m,
            "bounds_local": list(city.base_bounds),
        },
        "city": {
            "triangle_count": len(city.mesh.faces),
            "watertight": bool(city.mesh.is_watertight),
            "winding_consistent": bool(city.mesh.is_winding_consistent),
            "volume_m3": round(float(city.mesh.volume), 3),
            "non_manifold_edges": non_manifold_edges,
            "base_thickness_m": city.base_thickness_m,
            "union_engine": city.engine,
            "weld_repairs": city.weld_repairs,
        },
        "source": {
            "provider": "OpenStreetMap",
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "endpoint": overpass_meta.get("endpoint", ""),
            "query_hash": overpass_meta.get("query_hash", ""),
            "query_version": overpass_meta.get("query_version", ""),
            "fetched_at": overpass_meta.get("fetched_at", ""),
            "osm_base_timestamp": overpass_meta.get("osm_base_timestamp", ""),
        },
        "options": request.options.model_dump(),
        "request": request.model_dump(exclude={"request"}),
        "warnings": [warning.model_dump() for warning in warnings],
        "files": [],
    }
    return metadata


def _geojson(build_result: BuildResult) -> dict[str, Any]:
    features = []
    for record in build_result.records:
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(record.source_geometry),
                "properties": {
                    "osm_type": record.osm_type,
                    "osm_id": record.osm_id,
                    "height_m": round(record.height_m, 3),
                    "base_m": round(record.base_m, 3),
                    "height_source": record.height_source,
                    "area_m2": round(record.area_m2, 3),
                    "tags": _selected_tags(record.tags),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _readme_text(metadata: dict[str, Any]) -> str:
    origin = metadata["local_frame"]["origin"]
    city = metadata["city"]
    warnings = metadata.get("warnings") or []
    warning_lines = "\n".join(f"- [{item['code']}] {item['message']}" for item in warnings)
    if not warning_lines:
        warning_lines = "- None"
    return f"""CityGenerator output
===================

Units: metres
Local axes: +X east, +Y north, +Z up
Local origin: longitude {origin["lon"]}, latitude {origin["lat"]}
Projected CRS: {metadata["crs_projected"]}
OSM data timestamp: {metadata["source"]["osm_base_timestamp"] or "unknown"}

Files
-----
city.stl        One fused, watertight solid: all buildings plus a
                {city["base_thickness_m"]} m thick ground base.
                The boolean union removes internal faces and produces a
                single closed surface for CFD preprocessing.
metadata.json  Origin, CRS, parameters, statistics, warnings, and source.
buildings.geojson  WGS84 building footprints and height attributes.
README.txt      This file.

There are no separate buildings.stl or ground.stl files in this output.

CFD notes
---------
1. city.stl is one closed surface with no building/ground patch labels.
2. The ground base occupies z=-{city["base_thickness_m"]} to z=0; buildings
   extend below z=0 by {metadata["buildings"]["embed_depth_m"]} m before fusing.
3. Build the fluid domain and volume mesh in your solver; this tool does not
   generate a volume mesh or solver case.
4. The model uses metres and +Z up. Check the scale and wind direction before
   meshing.

Licence
-------
© OpenStreetMap contributors, ODbL 1.0
https://www.openstreetmap.org/copyright

Warnings
--------
{warning_lines}
"""


def _preview_mesh(build_result: BuildResult, city: FusedCity) -> trimesh.Trimesh:
    """Build a clean display-only mesh without boolean contact patches."""

    xmin, ymin, xmax, ymax = city.base_bounds
    thickness = city.base_thickness_m
    base = box(extents=(xmax - xmin, ymax - ymin, thickness))
    base.apply_translation(((xmin + xmax) / 2.0, (ymin + ymax) / 2.0, -thickness / 2.0))
    return trimesh.util.concatenate([*build_result.meshes, base])


def write_outputs(
    *,
    job_id: str,
    job_dir: Path,
    request: GenerateRequest,
    projector: Projector,
    build_result: BuildResult,
    overpass_meta: dict[str, Any],
    area_km2: float,
    city: FusedCity,
    settings: Settings,
    warnings: list[WarningItem],
) -> OutputBundle:
    output_dir = job_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    city_bytes = export_stl_bytes(city.mesh)
    write_bytes_atomic(output_dir / "city.stl", city_bytes)
    write_bytes_atomic(
        output_dir / "preview.stl",
        export_stl_bytes(_preview_mesh(build_result, city)),
    )
    names = ["city.stl"]

    loaded = load_stl_bytes(city_bytes)
    if len(loaded.faces) != len(city.mesh.faces):
        raise CityUnionError(
            "city.stl 回读后的三角形数与导出前不一致",
            detail=f"before={len(city.mesh.faces)}, after={len(loaded.faces)}",
        )
    if not loaded.is_watertight:
        raise CityUnionError("city.stl 回读后不再是水密表面")
    validate_city(loaded)
    written_non_manifold = non_manifold_edge_count(loaded)

    metadata = _build_metadata(
        job_id=job_id,
        request=request,
        projector=projector,
        build_result=build_result,
        non_manifold_edges=written_non_manifold,
        overpass_meta=overpass_meta,
        area_km2=area_km2,
        city=city,
        settings=settings,
        warnings=warnings,
    )
    metadata["files"] = names + ["metadata.json", "buildings.geojson", "README.txt"]

    write_json_atomic(output_dir / "metadata.json", metadata)
    write_json_atomic(output_dir / "buildings.geojson", _geojson(build_result))
    write_bytes_atomic(
        output_dir / "README.txt",
        _readme_text(metadata).encode("utf-8"),
    )
    names.extend(["metadata.json", "buildings.geojson", "README.txt"])

    zip_path = output_dir / "result.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(output_dir / name, arcname=name)

    files = [
        FileInfo(
            name=name,
            size=(output_dir / name).stat().st_size,
            url=f"/api/jobs/{job_id}/files/{name}",
        )
        for name in names
    ]
    return OutputBundle(
        files=files,
        zip_path=zip_path,
        zip_size=zip_path.stat().st_size,
        triangle_count=len(city.mesh.faces),
        metadata=metadata,
    )
