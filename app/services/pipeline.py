"""End-to-end pipeline: Overpass -> geometry -> STL bundle."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyproj import Geod

from app.config import Settings
from app.models.schemas import FileInfo, GenerateRequest, WarningItem
from app.services.buildings import (
    BuildCancelled,
    detect_overlaps,
    extrude_prepared,
    prepare_buildings,
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
from app.services.terrain import (
    ElevationSource,
    TerrainGrid,
    build_terrain_grid,
    build_terrain_solid,
)

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


def _terrain_percent(done: int, total: int) -> int:
    return 36 + int(4 * done / max(total, 1))


def run_pipeline(
    request: GenerateRequest,
    settings: Settings,
    job_dir: Path,
    *,
    progress_cb: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    elevation_source: ElevationSource | None = None,
) -> PipelineResult:
    progress = progress_cb or _noop_progress
    cancel = cancel_event or threading.Event()
    job_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    stage_started = started
    timings: dict[str, float] = {}

    def mark(stage: str) -> None:
        """Record the wall time since the previous stage boundary."""

        nonlocal stage_started
        now = time.perf_counter()
        timings[stage] = round(now - stage_started, 3)
        stage_started = now

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
    mark("validate")

    progress(4, "fetching", "从 Overpass 获取建筑数据")

    def overpass_progress(done: int, total: int) -> None:
        progress(
            4 + int(18 * done / max(total, 1)),
            "fetching",
            f"已下载 {done}/{total} 个 OSM 分块",
        )

    overpass = OverpassClient(settings)
    try:
        fetched = overpass.fetch(
            request.bbox,
            refresh=request.refresh,
            progress_cb=overpass_progress,
        )
    finally:
        overpass.close()
    write_json_atomic(job_dir / "raw_osm.json", fetched.raw)
    mark("fetch")
    check_cancel()

    progress(22, "parsing", "解析 OSM 建筑轮廓")
    features = parse_buildings(fetched.raw)
    if not features:
        raise NoBuildingsError("该区域没有可用的建筑数据")
    if len(features) > settings.max_buildings:
        raise TooManyBuildingsError(
            "建筑数量过多",
            f"当前 {len(features)} 栋，上限 {settings.max_buildings} 栋",
        )
    mark("parse")

    progress(30, "projecting", "投影到局部米制坐标")
    projector = projector_for_bbox(*request.bbox.as_tuple())
    check_cancel()
    mark("project")

    progress(34, "building", "清理建筑轮廓并解析高度")

    def prepare_progress(done: int, total: int) -> None:
        percent = 34 + int(2 * done / max(total, 1))
        progress(percent, "building", f"已清理 {done}/{total} 栋建筑轮廓")

    try:
        prepared = prepare_buildings(
            features,
            projector,
            default_height_m=request.options.default_height_m,
            level_height_m=request.options.level_height_m,
            simplify_m=request.options.simplify_m,
            min_building_area_m2=request.options.min_building_area_m2,
            sample_missing_heights=settings.height_sampling_enabled,
            height_jitter=settings.height_sampling_jitter,
            height_seed=settings.height_sampling_seed,
            min_height_samples=settings.height_sampling_min_samples,
            height_conservative=settings.height_sampling_conservative,
            height_slenderness_k=settings.height_sampling_slenderness_k,
            max_height_m=settings.max_height_m,
            height_overrides=request.options.height_overrides,
            progress_cb=prepare_progress,
            cancel_event=cancel,
        )
    except BuildCancelled as exc:
        raise JobCancelled("任务已取消") from exc

    if not prepared.items:
        raise NoBuildingsError("建筑轮廓均无效，未能生成任何模型")
    mark("prepare")

    bbox_bounds = bbox_local_bounds(projector, request)
    combined_bounds = _union_bounds(bbox_bounds, prepared.bounds)
    assert combined_bounds is not None
    margin = request.options.ground_margin_m
    base_bounds = (
        combined_bounds[0] - margin,
        combined_bounds[1] - margin,
        combined_bounds[2] + margin,
        combined_bounds[3] + margin,
    )

    terrain: TerrainGrid | None = None
    if request.options.terrain.enabled:
        progress(35, "terrain", "获取地形高程数据")

        taper = float(request.options.terrain.edge_taper_m)
        terrain_bounds = (
            base_bounds[0] - taper,
            base_bounds[1] - taper,
            base_bounds[2] + taper,
            base_bounds[3] + taper,
        )

        def terrain_progress(done: int, total: int) -> None:
            progress(_terrain_percent(done, total), "terrain", f"已获取 {done}/{total} 个高程瓦片")

        try:
            terrain = build_terrain_grid(
                terrain_bounds,
                projector,
                settings=settings,
                resolution_m=request.options.terrain.resolution_m,
                source=elevation_source,
                cancel_event=cancel,
                progress_cb=terrain_progress,
                core_bounds=base_bounds,
                edge_taper_m=taper,
            )
        except BuildCancelled as exc:
            raise JobCancelled("任务已取消") from exc
        check_cancel()
    mark("terrain")
    progress(40, "building", "挤出建筑并贴合地形")

    def build_progress(done: int, total: int) -> None:
        percent = 40 + int(38 * done / max(total, 1))
        progress(percent, "building", f"已处理 {done}/{total} 栋建筑")

    try:
        build_result = extrude_prepared(
            prepared,
            terrain=terrain,
            embed_depth_m=request.options.embed_depth_m,
            contact_overlap_m=settings.contact_overlap_m,
            progress_cb=build_progress,
            cancel_event=cancel,
        )
    except BuildCancelled as exc:
        raise JobCancelled("任务已取消") from exc
    mark("build")

    if not build_result.meshes:
        raise NoBuildingsError("建筑轮廓均无效，未能生成任何模型")

    warnings = [
        WarningItem(code=issue.code, message=issue.message, count=issue.count)
        for issue in build_result.warnings
    ]
    if terrain is not None:
        warnings.extend(
            WarningItem(code=issue.code, message=issue.message, count=issue.count)
            for issue in terrain.warnings
        )
    overlap_count = detect_overlaps([record.polygon_local for record in build_result.records])
    if overlap_count:
        warnings.append(
            WarningItem(
                code="OVERLAP_DETECTED",
                message=f"检测到 {overlap_count} 对建筑轮廓重叠，融合时已按并集处理",
                count=overlap_count,
            )
        )

    check_cancel()
    progress(80, "ground", "生成实体地形底座" if terrain is not None else "生成实体地面底座")

    if terrain is not None:
        base = build_terrain_solid(terrain, thickness_m=settings.ground_slab_thickness_m)
    else:
        base = build_ground(
            base_bounds,
            mode="slab",
            thickness_m=settings.ground_slab_thickness_m,
        )
    mark("ground")

    check_cancel()
    progress(84, "fusing", "融合建筑与地面")
    city = fuse_city(
        build_result.meshes,
        base,
        base_thickness_m=settings.ground_slab_thickness_m,
        check_input_volumes=False,
    )
    mark("fuse")

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
        terrain=terrain,
        timings=timings,
        started_at=started,
    )

    stats: dict[str, Any] = {
        "buildings": len(build_result.records),
        "triangles": len(city.mesh.faces),
        "city_triangles": len(city.mesh.faces),
        "city_watertight": bool(city.mesh.is_watertight),
        "base_thickness_m": city.base_thickness_m,
        "weld_repairs": city.weld_repairs,
        "height_sources": dict(build_result.height_sources),
        "skipped": build_result.skipped,
        "overlaps": overlap_count,
        "area_km2": round(area, 4),
        "patch_counts": dict(bundle.metadata.get("patches", {}).get("counts", {})),
        "patch_ranges": int(bundle.metadata.get("patches", {}).get("range_count", 0)),
        "from_cache": fetched.from_cache,
        "osm_base_timestamp": fetched.meta.get("osm_base_timestamp", ""),
        "terrain_enabled": terrain is not None,
        "terrain_relief_m": round(terrain.relief_m, 3) if terrain is not None else 0.0,
        "terrain_resolution_m": round(terrain.resolution_m, 2) if terrain is not None else 0.0,
        "terrain_source": str(terrain.meta.get("source", "")) if terrain is not None else "flat",
        "terrain_triangles": terrain.triangle_count if terrain is not None else 0,
        "timings_s": timings,
    }
    progress(100, "completed", "生成完成")

    return PipelineResult(
        stats=stats,
        warnings=warnings,
        files=bundle.files,
        zip_path=bundle.zip_path,
        zip_name=f"citygenerator_{job_dir.name}.zip",
    )
