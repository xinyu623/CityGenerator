"""Building footprint cleanup, extrusion, and batch mesh construction."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import trimesh
from shapely import STRtree, make_valid
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient

from app.services.heights import HeightInfo, resolve_height
from app.services.projection import Projector

if TYPE_CHECKING:
    from app.services.osm_parse import BuildingFeature


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    count: int = 1


@dataclass
class BuildingRecord:
    osm_type: str
    osm_id: int
    tags: dict[str, str]
    source_geometry: Polygon | MultiPolygon
    polygon_local: Polygon
    height_m: float
    base_m: float
    height_source: str
    area_m2: float
    mesh: trimesh.Trimesh


@dataclass
class BuildResult:
    meshes: list[trimesh.Trimesh] = field(default_factory=list)
    records: list[BuildingRecord] = field(default_factory=list)
    bounds: tuple[float, float, float, float] | None = None
    height_sources: dict[str, int] = field(default_factory=dict)
    skipped: int = 0
    warnings: list[Issue] = field(default_factory=list)


class BuildCancelled(RuntimeError):
    """Raised when the caller cancels a running build."""


def _extract_polygons(geometry: object) -> list[Polygon]:
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    geoms = getattr(geometry, "geoms", None)
    if geoms is not None:
        result: list[Polygon] = []
        for part in geoms:
            result.extend(_extract_polygons(part))
        return result
    return []


def clean_polygons(
    geometry: object,
    simplify_m: float = 0.0,
    min_area_m2: float = 4.0,
    min_hole_area_m2: float = 1.0,
) -> list[Polygon]:
    """Fix invalid rings, orient them, drop slivers, and optionally simplify."""

    if geometry is None:
        return []

    geom: object = geometry
    try:
        if not geom.is_valid:  # type: ignore[attr-defined]
            geom = make_valid(geom)
    except Exception:
        try:
            geom = geom.buffer(0)  # type: ignore[attr-defined]
        except Exception:
            return []

    try:
        if geom.is_empty:  # type: ignore[attr-defined]
            return []
    except Exception:
        return []

    polygons = _extract_polygons(geom)
    cleaned: list[Polygon] = []
    for polygon in polygons:
        if polygon.is_empty or not polygon.is_valid:
            polygon = _repair_polygon(polygon)
            if polygon is None:
                continue

        polygon = orient(polygon, sign=1.0)
        holes = []
        for ring in polygon.interiors:
            try:
                hole = Polygon(ring)
            except Exception:
                continue
            if hole.is_valid and hole.area >= min_hole_area_m2:
                holes.append(ring)
        if len(holes) != len(polygon.interiors):
            polygon = Polygon(polygon.exterior, holes)

        if simplify_m > 0.0:
            simplified = polygon.simplify(simplify_m, preserve_topology=True)
            if (
                isinstance(simplified, Polygon)
                and not simplified.is_empty
                and simplified.is_valid
                and simplified.area >= min_area_m2
            ):
                polygon = simplified

        polygon = orient(polygon, sign=1.0)
        if polygon.area < min_area_m2:
            continue
        cleaned.append(polygon)
    return cleaned


def _repair_polygon(polygon: Polygon) -> Polygon | None:
    try:
        repaired = make_valid(polygon)
    except Exception:
        try:
            repaired = polygon.buffer(0)
        except Exception:
            return None
    polygons = _extract_polygons(repaired)
    return polygons[0] if polygons else None


def project_polygon(polygon: Polygon, projector: Projector) -> Polygon:
    """Project one WGS84 polygon into the local metric frame."""

    def ring(coords: Iterable[tuple[float, ...]]) -> list[tuple[float, float]]:
        array = np.asarray(list(coords), dtype=float)
        easting, northing = projector.transformer.transform(array[:, 0], array[:, 1])
        return [
            projector.to_local(float(x), float(y))
            for x, y in zip(easting, northing, strict=True)
        ]

    exterior = ring(polygon.exterior.coords)
    interiors = [ring(interior.coords) for interior in polygon.interiors]
    return Polygon(exterior, interiors)


def project_geometry(geometry: Polygon | MultiPolygon, projector: Projector) -> Polygon | MultiPolygon:
    if isinstance(geometry, Polygon):
        return project_polygon(geometry, projector)
    return MultiPolygon([project_polygon(part, projector) for part in geometry.geoms])


def extrude_building(
    polygon: Polygon,
    height_m: float,
    base_m: float = 0.0,
    embed_depth_m: float = 0.5,
) -> trimesh.Trimesh:
    """Extrude a valid local-metric polygon into a closed building shell."""

    height_above_base = float(height_m) - float(base_m)
    if height_above_base <= 0.0:
        raise ValueError("height must be greater than base")

    extrusion_height = height_above_base
    if base_m <= 0.0 and embed_depth_m > 0.0:
        # Extend the shell below the ground plane so that the building's side
        # walls do not share a coplanar face with the ground patch.
        extrusion_height += float(embed_depth_m)

    mesh = trimesh.creation.extrude_polygon(polygon, extrusion_height)
    if mesh is None or len(mesh.faces) == 0:
        raise ValueError("triangulation produced an empty mesh")

    if base_m > 0.0:
        mesh.apply_translation((0.0, 0.0, float(base_m)))
    elif embed_depth_m > 0.0:
        mesh.apply_translation((0.0, 0.0, -float(embed_depth_m)))

    mesh = mesh.copy()
    mesh.update_faces(mesh.nondegenerate_faces())
    if hasattr(mesh, "remove_unreferenced_vertices"):
        mesh.remove_unreferenced_vertices()
    trimesh.repair.fix_winding(mesh)
    trimesh.repair.fix_normals(mesh)
    return mesh


def _mesh_is_valid(mesh: trimesh.Trimesh) -> bool:
    try:
        return bool(
            mesh.is_watertight
            and mesh.is_winding_consistent
            and len(mesh.faces) > 0
            and mesh.volume > 0.0
            and np.isfinite(mesh.vertices).all()
        )
    except Exception:
        return False


def _update_bounds(
    bounds: tuple[float, float, float, float] | None,
    polygon: Polygon,
) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = polygon.bounds
    if bounds is None:
        return (float(minx), float(miny), float(maxx), float(maxy))
    return (
        min(bounds[0], float(minx)),
        min(bounds[1], float(miny)),
        max(bounds[2], float(maxx)),
        max(bounds[3], float(maxy)),
    )


def detect_overlaps(polygons: Sequence[Polygon], min_overlap_m2: float = 0.5) -> int:
    """Count pairs of footprints whose intersection area is significant.

    Touching footprints (shared walls) have zero intersection area and are not
    counted. The function deliberately does not union anything: merging row
    houses would destroy per-building heights.
    """

    if len(polygons) < 2:
        return 0
    tree = STRtree(list(polygons))
    pairs = tree.query(list(polygons), predicate="intersects")
    count = 0
    for left, right in zip(pairs[0], pairs[1], strict=False):
        if left >= right:
            continue
        try:
            area = polygons[left].intersection(polygons[right]).area
        except Exception:
            continue
        if area > min_overlap_m2:
            count += 1
    return count


def build_meshes(
    features: Sequence[BuildingFeature],
    projector: Projector,
    *,
    default_height_m: float = 9.0,
    level_height_m: float = 3.0,
    embed_depth_m: float = 0.5,
    simplify_m: float = 0.05,
    min_building_area_m2: float = 4.0,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> BuildResult:
    """Project, clean, and extrude every building feature."""

    result = BuildResult()
    counts: Counter[str] = Counter()
    total = len(features)

    for index, feature in enumerate(features):
        if cancel_event is not None and cancel_event.is_set():
            raise BuildCancelled("job cancelled")

        try:
            geometry = project_geometry(feature.geometry, projector)
        except Exception:
            counts["INVALID_GEOMETRY"] += 1
            result.skipped += 1
            continue

        polygons = clean_polygons(
            geometry,
            simplify_m=simplify_m,
            min_area_m2=min_building_area_m2,
        )
        if not polygons:
            counts["INVALID_GEOMETRY"] += 1
            result.skipped += 1
            continue

        height: HeightInfo = resolve_height(
            feature.tags,
            default_height_m=default_height_m,
            level_height_m=level_height_m,
        )
        for polygon in polygons:
            result.bounds = _update_bounds(result.bounds, polygon)
            try:
                mesh = extrude_building(
                    polygon,
                    height_m=height.height_m,
                    base_m=height.base_m,
                    embed_depth_m=embed_depth_m,
                )
            except Exception:
                counts["TRIANGULATION_FAILED"] += 1
                result.skipped += 1
                continue

            if not _mesh_is_valid(mesh):
                counts["INVALID_MESH"] += 1
                result.skipped += 1
                continue

            result.meshes.append(mesh)
            result.records.append(
                BuildingRecord(
                    osm_type=feature.osm_type,
                    osm_id=feature.osm_id,
                    tags=dict(feature.tags),
                    source_geometry=feature.geometry,
                    polygon_local=polygon,
                    height_m=height.height_m,
                    base_m=height.base_m,
                    height_source=height.source,
                    area_m2=float(polygon.area),
                    mesh=mesh,
                )
            )
            counts[f"HEIGHT_{height.source}"] += 1
            result.height_sources[height.source] = result.height_sources.get(height.source, 0) + 1
            if height.clamped:
                counts["HEIGHT_CLAMPED"] += 1

        if progress_cb is not None:
            progress_cb(index + 1, total)

    if result.skipped:
        if counts["INVALID_GEOMETRY"]:
            result.warnings.append(
                Issue(
                    code="INVALID_GEOMETRY",
                    message=f"{counts['INVALID_GEOMETRY']} 个建筑轮廓无效或面积过小，已跳过",
                    count=counts["INVALID_GEOMETRY"],
                )
            )
        if counts["TRIANGULATION_FAILED"]:
            result.warnings.append(
                Issue(
                    code="TRIANGULATION_FAILED",
                    message=f"{counts['TRIANGULATION_FAILED']} 栋建筑三角化失败，已跳过",
                    count=counts["TRIANGULATION_FAILED"],
                )
            )
        if counts["INVALID_MESH"]:
            result.warnings.append(
                Issue(
                    code="INVALID_MESH",
                    message=f"{counts['INVALID_MESH']} 栋建筑生成结果非水密，已跳过",
                    count=counts["INVALID_MESH"],
                )
            )
    if result.height_sources.get("default"):
        result.warnings.append(
            Issue(
                code="MISSING_HEIGHT",
                message=(
                    f"{result.height_sources['default']} 栋建筑缺少高度标签，"
                    f"已使用默认高度 {default_height_m:g} m"
                ),
                count=result.height_sources["default"],
            )
        )
    if counts["HEIGHT_CLAMPED"]:
        result.warnings.append(
            Issue(
                code="HEIGHT_CLAMPED",
                message=f"{counts['HEIGHT_CLAMPED']} 栋建筑高度超出允许范围，已截断",
                count=counts["HEIGHT_CLAMPED"],
            )
        )

    return result
