"""End-to-end pipeline: Overpass -> geometry -> STL bundle."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyproj import Geod

from app.config import Settings
from app.models.schemas import FileInfo, GenerateRequest, WarningItem
from app.services.buildings import (
    BuildCancelled,
    build_meshes,
    detect_overlaps,
)
from app.services.city import fuse_city
from app.services.errors import (
    AreaTooLargeError,
    JobCancelled,
    NoBuildingsError,
    TooManyBuildingsError,
)
from app.services.ground import build_ground
from app.services.io_utils import write_json_atomic
from app.services.osm_parse import parse_buildings
from app.services.output import write_outputs
from app.services.overpass import OverpassClient
from app.services.projection import Projector, projector_for_bbox

ProgressCallback = Callable[[int, str, str], None]


@dataclass
class PipelineResult:
    stats: dict[str, Any]
    warnings: list[WarningItem]
    files: list[FileInfo]
    zip_path: Path
    zip_name: str


def bbox_area_km2(request: GenerateRequest) -> float:
    bbox = request.bbox
    geod = Geod(ellps="WGS84")
    lons = [bbox.west, bbox.east, bbox.east, bbox.west]
    lats = [bbox.south, bbox.south, bbox.north, bbox.north]
    area, _ = geod.polygon_area_perimeter(lons, lats)
    return abs(float(area)) / 1_000_000.0


def bbox_local_bounds(
    projector: Projector, request: GenerateRequest
) -> tuple[float, float, float, float]:
    bbox = request.bbox
    corners = [
        (bbox.west, bbox.south),
        (bbox.east, bbox.south),
        (bbox.east, bbox.north),
        (bbox.west, bbox.north),
    ]
    xs: list[float] = []
    ys: list[float] = []
    for lon, lat in corners:
        easting, northing = projector.project(lon, lat)
        x, y = projector.to_local(easting, northing)
        xs.append(x)
        ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys))


def _union_bounds(
    first: tuple[float, float, float, float] | None,
    second: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    if first is None:
        return second
    if second is None:
        return first
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _noop_progress(percent: int, stage: str, message: str) -> None:
    del percent, stage, message


def run_pipeline(
    request: GenerateRequest,
    settings: Settings,
    job_dir: Path,
    *,
    progress_cb: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> PipelineResult:
    progress = progress_cb or _noop_progress
    cancel = cancel_event or threading.Event()
    job_dir.mkdir(parents=True, exist_ok=True)

    def check_cancel() -> None:
        if cancel.is_set():
            raise JobCancelled("任务已取消")

    progress(0, "validating", "校验选择区域")
    area = bbox_area_km2(request)
    if area > settings.max_area_km2:
        raise AreaTooLargeError(
            "选择区域过大",
            f"当前 {area:.2f} km²，上限 {settings.max_area_km2:g} km²",
        )

    progress(4, "fetching", "从 Overpass 获取建筑数据")
    overpass = OverpassClient(settings)
    try:
        fetched = overpass.fetch(request.bbox, refresh=request.refresh)
    finally:
        overpass.close()
    write_json_atomic(job_dir / "raw_osm.json", fetched.raw)
    check_cancel()

    progress(24, "parsing", "解析 OSM 建筑轮廓")
    features = parse_buildings(fetched.raw)
    if not features:
        raise NoBuildingsError("该区域没有可用的建筑数据")
    if len(features) > settings.max_buildings:
        raise TooManyBuildingsError(
            "建筑数量过多",
            f"当前 {len(features)} 栋，上限 {settings.max_buildings} 栋",
        )

    progress(34, "projecting", "投影到局部米制坐标")
    projector = projector_for_bbox(*request.bbox.as_tuple())
    check_cancel()

    progress(40, "building", "清理并挤出建筑")

    def build_progress(done: int, total: int) -> None:
        percent = 40 + int(40 * done / max(total, 1))
        progress(percent, "building", f"已处理 {done}/{total} 栋建筑")

    try:
        build_result = build_meshes(
            features,
            projector,
            default_height_m=request.options.default_height_m,
            level_height_m=request.options.level_height_m,
            embed_depth_m=request.options.embed_depth_m,
            simplify_m=request.options.simplify_m,
            min_building_area_m2=request.options.min_building_area_m2,
            progress_cb=build_progress,
            cancel_event=cancel,
        )
    except BuildCancelled as exc:
        raise JobCancelled("任务已取消") from exc

    if not build_result.meshes:
        raise NoBuildingsError("建筑轮廓均无效，未能生成任何模型")

    warnings = [
        WarningItem(code=issue.code, message=issue.message, count=issue.count)
        for issue in build_result.warnings
    ]
    overlap_count = detect_overlaps([record.polygon_local for record in build_result.records])
    if overlap_count:
        warnings.append(
            WarningItem(
                code="OVERLAP_DETECTED",
                message=f"检测到 {overlap_count} 对建筑轮廓重叠，已保留原几何，请按需处理",
                count=overlap_count,
            )
        )

    check_cancel()
    progress(82, "ground", "生成实体地面底座")

    bbox_bounds = bbox_local_bounds(projector, request)
    combined_bounds = _union_bounds(bbox_bounds, build_result.bounds)
    assert combined_bounds is not None
    margin = request.options.ground_margin_m
    base_bounds = (
        combined_bounds[0] - margin,
        combined_bounds[1] - margin,
        combined_bounds[2] + margin,
        combined_bounds[3] + margin,
    )
    base = build_ground(
        base_bounds,
        mode="slab",
        thickness_m=settings.ground_slab_thickness_m,
    )

    check_cancel()
    progress(86, "fusing", "融合建筑与地面")
    city = fuse_city(build_result.meshes, base)

    check_cancel()
    progress(90, "exporting", "写出 STL、元数据和下载包")

    bundle = write_outputs(
        job_id=job_dir.name,
        job_dir=job_dir,
        request=request,
        projector=projector,
        build_result=build_result,
        overpass_meta=fetched.meta,
        area_km2=area,
        city=city,
        settings=settings,
        warnings=warnings,
    )

    stats: dict[str, Any] = {
        "buildings": len(build_result.records),
        "triangles": len(city.mesh.faces),
        "city_triangles": len(city.mesh.faces),
        "city_watertight": bool(city.mesh.is_watertight),
        "base_thickness_m": city.base_thickness_m,
        "height_sources": dict(build_result.height_sources),
        "skipped": build_result.skipped,
        "overlaps": overlap_count,
        "area_km2": round(area, 4),
        "from_cache": fetched.from_cache,
        "osm_base_timestamp": fetched.meta.get("osm_base_timestamp", ""),
    }
    progress(100, "completed", "生成完成")

    return PipelineResult(
        stats=stats,
        warnings=warnings,
        files=bundle.files,
        zip_path=bundle.zip_path,
        zip_name=f"citygenerator_{job_dir.name}.zip",
    )
