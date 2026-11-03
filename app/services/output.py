"""Write the fused city STL, metadata, GeoJSON, README, and download ZIP."""

from __future__ import annotations

import time
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
from app.services.patches import face_kinds, patch_table
from app.services.projection import Projector
from app.services.stl_export import export_stl_bytes, load_stl_bytes
from app.services.terrain import TerrainGrid

SCHEMA_VERSION = "2.0"


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
    terrain: TerrainGrid | None = None,
    patches: dict[str, Any] | None = None,
) -> dict[str, Any]:
    origin_lon, origin_lat = projector.origin_wgs84()
    heights = [record.height_m for record in build_result.records]
    z_reference = (
        "lowest terrain cell in the selection" if terrain is not None else "ground plane at z=0"
    )
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
            "z_reference": z_reference,
            "origin_mode": "min_xy" if settings.origin_at_min_xy else "bbox_sw",
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
            "contact_overlap_m": settings.contact_overlap_m,
            "height_sampling": build_result.height_sampling,
            "each_shell_watertight": build_result.each_shell_watertight,
        },
        "ground": {
            "included": True,
            "mode": "terrain" if terrain is not None else "slab",
            "surface": terrain.meta.get("source", "dem") if terrain is not None else "flat",
            "z": 0.0,
            "thickness_m": city.base_thickness_m,
            "bounds_local": list(city.base_bounds),
        },
        "terrain": _terrain_metadata(build_result, terrain),
        "patches": {
            "file": "patches.json",
            "unit": "m",
            "triangle_count": int(patches["triangle_count"]) if patches else 0,
            "ground_tolerance_m": float(patches["ground_tolerance_m"]) if patches else 0.0,
            "ground_normal_dot_min": float(patches["ground_normal_dot_min"]) if patches else 0.0,
            "counts": dict(patches["counts"]) if patches else {},
            "range_count": len(patches["ranges"]) if patches else 0,
        },
        "city": {
            "triangle_count": len(city.mesh.faces),
            "watertight": bool(city.mesh.is_watertight),
            "winding_consistent": bool(city.mesh.is_winding_consistent),
            "volume_m3": round(float(city.mesh.volume), 3),
            "non_manifold_edges": non_manifold_edges,
            "base_thickness_m": city.base_thickness_m,
            "terrain_triangle_count": terrain.triangle_count if terrain is not None else 0,
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
            "overpass_chunks": int(overpass_meta.get("chunks", 1)),
            "overpass_splits": overpass_meta.get("overpass_splits", []),
            "overpass_cell_times": overpass_meta.get("cell_times", []),
        },
        "options": request.options.model_dump(),
        "request": request.model_dump(exclude={"request"}),
        "warnings": [warning.model_dump() for warning in warnings],
        "files": [],
    }
    return metadata


def _terrain_metadata(
    build_result: BuildResult, terrain: TerrainGrid | None
) -> dict[str, Any]:
    if terrain is None:
        return {"enabled": False}
    reliefs = [record.footprint_relief_m for record in build_result.records]
    described = terrain.describe()
    described.update(
        {
            "enabled": True,
            "max_footprint_relief_m": round(max(reliefs), 3) if reliefs else 0.0,
            "building_base_strategy": "min over the footprint's covering cells",
            "attribution": "AWS Terrain Tiles (Mapzen / Amazon, SRTM derived)",
        }
    )
    return described


def _geojson(build_result: BuildResult) -> dict[str, Any]:
    features = []
    for record in build_result.records:
        properties: dict[str, Any] = {
            "osm_type": record.osm_type,
            "osm_id": record.osm_id,
            "height_m": round(record.height_m, 3),
            "base_m": round(record.base_m, 3),
            "height_source": record.height_source,
            "area_m2": round(record.area_m2, 3),
            "ground_m": round(record.ground_m, 3),
            "roof_m": round(record.ground_m + record.height_m, 3),
            "footprint_relief_m": round(record.footprint_relief_m, 3),
            "tags": _selected_tags(record.tags),
        }
        if record.height_rule:
            properties["height_rule"] = record.height_rule
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(record.source_geometry),
                "properties": properties,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _readme_text(metadata: dict[str, Any]) -> str:
    origin = metadata["local_frame"]["origin"]
    city = metadata["city"]
    warnings = metadata.get("warnings") or []
    terrain_block, ground_note = _terrain_readme(metadata)
    patch_block = _patch_readme(metadata)
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
patches.json   Contiguous triangle ranges of city.stl labelled ground, wall,
               or bottom, for CFD patch assignment.
buildings.geojson  WGS84 building footprints and height attributes.
README.txt      This file.

There are no separate buildings.stl or ground.stl files in this output.

{terrain_block}
{patch_block}
CFD notes
---------
1. city.stl is one closed surface; patches.json labels its triangles as
   ground, wall, or bottom in the same order as the STL file.
2. {ground_note}
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


def _terrain_readme(metadata: dict[str, Any]) -> tuple[str, str]:
    """Return the terrain section and the ground note for README.txt."""

    terrain = metadata.get("terrain") or {}
    thickness = metadata["city"]["base_thickness_m"]
    embed = metadata["buildings"]["embed_depth_m"]
    if not terrain.get("enabled"):
        note = (
            f"The flat ground base occupies z=-{thickness} to z=0; buildings extend "
            f"below z=0 by {embed} m before fusing."
        )
        return "", note

    relief = terrain.get("relief_m", 0.0)
    datum = terrain.get("datum_offset_m", 0.0)
    resolution = terrain.get("resolution_m", 0.0)
    section = (
        "Terrain\n"
        "-------\n"
        f"Source: {terrain.get('attribution', 'AWS Terrain Tiles')}\n"
        f"Grid: {resolution} m, relief {relief} m, DEM zoom {terrain.get('zoom')} "
        f"({terrain.get('pixel_m')} m/px)\n"
        f"z=0 is the lowest terrain cell in the selection; add {datum} m to restore\n"
        "absolute elevations from the source DEM.\n"
        "Each building sits on the lowest terrain cell under its footprint and is\n"
        f"embedded {embed} m below that height before the union.\n"
        "\n"
    )
    note = (
        f"The terrain solid occupies z=-{thickness} to z={relief}; buildings are "
        f"embedded {embed} m below the local terrain before fusing."
    )
    return section, note


def _patch_readme(metadata: dict[str, Any]) -> str:
    """Return the patch-groups section for README.txt."""

    patches = metadata.get("patches") or {}
    total = patches.get("triangle_count", 0)
    if not total:
        return ""
    counts = patches.get("counts") or {}
    parts = ", ".join(
        f"{name} {counts.get(name, 0)}" for name in ("ground", "wall", "bottom")
    )
    return (
        "Patch groups\n"
        "------------\n"
        f"patches.json maps all {total} triangles of city.stl to ground, wall, or\n"
        f"bottom ({parts}). The ranges are contiguous and in STL order, so a solver\n"
        "can assign boundary patches by walking the file once.\n"
        f"A triangle is ground when it faces up, its centroid and all three vertices\n"
        f"are within {patches.get('ground_tolerance_m')} m of the terrain surface, and its\n"
        f"normal agrees with the terrain normal (dot >= {patches.get('ground_normal_dot_min')}).\n"
        "Other upward triangles are roofs and stay walls. The terrain skirt is a\n"
        "wall and the base underside is bottom.\n"
        "\n"
    )


def _preview_mesh(build_result: BuildResult, city: FusedCity) -> trimesh.Trimesh:
    """Build a clean display-only mesh without boolean contact patches."""

    if city.base_mesh is not None:
        base = city.base_mesh
    else:  # pragma: no cover - defensive fallback for hand-built FusedCity values
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
    terrain: TerrainGrid | None = None,
    timings: dict[str, float] | None = None,
    started_at: float | None = None,
) -> OutputBundle:
    output_dir = job_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    export_started = time.perf_counter()
    pipeline_started = export_started if started_at is None else started_at

    # fuse_city already exported the city and reloaded it to prove the STL is
    # closed; reuse those bytes and that mesh instead of a second round-trip.
    # The fallback for a hand-built FusedCity exports, loads, and validates
    # before anything is written, so a failure never leaves a bad city.stl.
    stl_bytes, stl_mesh = city.stl_bytes, city.stl_mesh
    if stl_bytes is not None and stl_mesh is not None:
        city_bytes = stl_bytes
        loaded = stl_mesh
        written_non_manifold = 0
    else:
        city_bytes = export_stl_bytes(city.mesh)
        loaded = load_stl_bytes(city_bytes)
        if not loaded.is_watertight:
            raise CityUnionError("city.stl 回读后不再是水密表面")
        written_non_manifold = validate_city(loaded)
    if len(loaded.faces) != len(city.mesh.faces):
        raise CityUnionError(
            "city.stl 回读后的三角形数与导出前不一致",
            detail=f"before={len(city.mesh.faces)}, after={len(loaded.faces)}",
        )
    write_bytes_atomic(output_dir / "city.stl", city_bytes)
    write_bytes_atomic(
        output_dir / "preview.stl",
        export_stl_bytes(_preview_mesh(build_result, city)),
    )

    kinds = face_kinds(
        loaded,
        terrain=terrain,
        ground_tolerance_m=settings.patch_ground_tolerance_m,
        normal_dot_min=settings.patch_ground_normal_dot_min,
    )
    patches = patch_table(
        kinds,
        ground_tolerance_m=settings.patch_ground_tolerance_m,
        normal_dot_min=settings.patch_ground_normal_dot_min,
    )
    write_json_atomic(output_dir / "patches.json", patches)
    names = ["city.stl", "patches.json"]

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
        terrain=terrain,
        patches=patches,
    )
    metadata["files"] = [*names, "metadata.json", "buildings.geojson", "README.txt"]

    write_json_atomic(output_dir / "buildings.geojson", _geojson(build_result))
    write_bytes_atomic(
        output_dir / "README.txt",
        _readme_text(metadata).encode("utf-8"),
    )
    names.extend(["metadata.json", "buildings.geojson", "README.txt"])

    if timings is not None:
        # Timings are a snapshot taken just before metadata.json is written: the
        # metadata write itself and the result zip come after.
        timings["export"] = round(time.perf_counter() - export_started, 3)
        timings["total"] = round(time.perf_counter() - pipeline_started, 3)
    metadata["timings_s"] = timings or {}
    write_json_atomic(output_dir / "metadata.json", metadata)

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
