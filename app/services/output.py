"""Write STL, metadata, GeoJSON, README, and the download ZIP."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import trimesh
from shapely.geometry import mapping

from app.config import Settings
from app.models.schemas import FileInfo, GenerateRequest, WarningItem
from app.services.buildings import BuildResult
from app.services.io_utils import utc_now_iso, write_bytes_atomic, write_json_atomic
from app.services.projection import Projector
from app.services.stl_export import export_stl_bytes, load_stl_bytes

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
    overpass_meta: dict[str, Any],
    area_km2: float,
    ground_bounds: tuple[float, float, float, float] | None,
    ground: trimesh.Trimesh | None,
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
            "footprint_area_m2": round(
                sum(record.area_m2 for record in build_result.records), 3
            ),
            "default_height_m": request.options.default_height_m,
            "level_height_m": request.options.level_height_m,
            "embed_depth_m": request.options.embed_depth_m,
        },
        "ground": {
            "included": ground is not None,
            "mode": request.options.ground_mode if ground is not None else "none",
            "z": 0.0,
            "thickness_m": settings.ground_slab_thickness_m,
            "bounds_local": list(ground_bounds) if ground_bounds else None,
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
    ground = metadata["ground"]
    warnings = metadata.get("warnings") or []
    warning_lines = "\n".join(f"- [{item['code']}] {item['message']}" for item in warnings)
    if not warning_lines:
        warning_lines = "- 无"
    return f"""CityGenerator 输出说明
======================

单位：米
局部坐标轴：+X 向东，+Y 向北，+Z 向上
局部原点：经度 {origin['lon']}，纬度 {origin['lat']}
投影坐标系：{metadata['crs_projected']}
OSM 数据时间：{metadata['source']['osm_base_timestamp'] or '未知'}

文件说明
--------
buildings.stl   所有建筑壳体。每栋建筑是封闭、法向朝外的多面体，适合作为 CFD 障碍物。
ground.stl      地面 patch，位于 z=0（{'薄板' if ground['mode'] == 'slab' else '平面'}）。
city.stl        buildings + ground 的拼接结果，便于预览和快速导入。
                注意：它是多个壳体的拼接，不是单一水密实体；CFD 前处理建议使用分文件。
metadata.json   坐标原点、投影、参数、统计、警告和数据来源。
buildings.geojson  WGS84 建筑轮廓和高度属性，可用于 GIS 核对。

CFD 导入建议
------------
1. 先分别导入 buildings.stl 和 ground.stl，并把它们指定为不同 patch。
2. 建筑底部默认向地面以下延伸 {metadata['buildings']['embed_depth_m']} m，避免与地面共面。
3. 在求解器中按需要建立流体域并划分体网格；本工具不生成体网格或算例文件。
4. 如果求解器要求单一封闭表面，请先在你的前处理软件中做 surface wrap / boolean 处理。
5. 检查模型尺度和朝向：风场入口方向可参考 +X（东）或 +Y（北）。

数据许可
--------
© OpenStreetMap contributors, ODbL 1.0
https://www.openstreetmap.org/copyright

警告
----
{warning_lines}
"""


def write_outputs(
    *,
    job_id: str,
    job_dir: Path,
    request: GenerateRequest,
    projector: Projector,
    build_result: BuildResult,
    overpass_meta: dict[str, Any],
    area_km2: float,
    ground_bounds: tuple[float, float, float, float] | None,
    ground: trimesh.Trimesh | None,
    settings: Settings,
    warnings: list[WarningItem],
) -> OutputBundle:
    output_dir = job_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    building_mesh = trimesh.util.concatenate(build_result.meshes)
    meshes_to_join = [building_mesh]
    if ground is not None:
        meshes_to_join.append(ground)
    city_mesh = trimesh.util.concatenate(meshes_to_join)

    export_mode = request.options.export_mode
    names: list[str] = []

    if export_mode in {"both", "separate"}:
        building_bytes = export_stl_bytes(building_mesh)
        write_bytes_atomic(output_dir / "buildings.stl", building_bytes)
        names.append("buildings.stl")
        try:
            loaded = load_stl_bytes(building_bytes)
            if len(loaded.faces) != len(building_mesh.faces):
                warnings.append(
                    WarningItem(
                        code="STL_ROUNDTRIP_MISMATCH",
                        message="buildings.stl 回读后的三角形数与导出前不一致",
                    )
                )
            elif not loaded.is_watertight:
                warnings.append(
                    WarningItem(
                        code="STL_NOT_WATERTIGHT",
                        message="buildings.stl 回读后不是水密网格，请检查异常建筑",
                    )
                )
        except Exception as exc:
            warnings.append(
                WarningItem(
                    code="STL_VALIDATION_FAILED",
                    message=f"buildings.stl 回读校验失败：{exc}",
                )
            )

    if ground is not None and export_mode != "combined":
        write_bytes_atomic(output_dir / "ground.stl", export_stl_bytes(ground))
        names.append("ground.stl")

    if export_mode in {"both", "combined"}:
        write_bytes_atomic(output_dir / "city.stl", export_stl_bytes(city_mesh))
        names.append("city.stl")

    metadata = _build_metadata(
        job_id=job_id,
        request=request,
        projector=projector,
        build_result=build_result,
        overpass_meta=overpass_meta,
        area_km2=area_km2,
        ground_bounds=ground_bounds,
        ground=ground,
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
    triangle_count = len(building_mesh.faces) + (len(ground.faces) if ground is not None else 0)
    return OutputBundle(
        files=files,
        zip_path=zip_path,
        zip_size=zip_path.stat().st_size,
        triangle_count=triangle_count,
        metadata=metadata,
    )
