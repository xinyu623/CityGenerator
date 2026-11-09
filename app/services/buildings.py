"""Building footprint cleanup, extrusion, and batch mesh construction.

The batch is split in two phases because terrain sampling needs to know the
full extent of the footprints before any building base can be placed:

1. ``prepare_buildings`` projects, cleans, and resolves heights.
2. ``extrude_prepared`` places each shell at its terrain height and builds meshes.
"""

from __future__ import annotations

import math
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import trimesh
from shapely import STRtree, make_valid
from shapely.affinity import translate
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient
from trimesh import grouping
from trimesh.constants import tol as TRIMESH_TOL
from trimesh.creation import triangulate_polygon
from trimesh.geometry import faces_to_edges
from trimesh.triangles import extents as triangle_extents

from app.services.heights import (
    AREA_BANDWIDTH_LOG,
    AREA_RATIO_MAX,
    AREA_RATIO_MIN,
    GROUP_WEIGHT,
    TYPE_WEIGHT,
    WIDTH_SLENDERNESS_K,
    HeightInfo,
    HeightSampler,
    KnownHeight,
    building_type_group,
    resolve_height,
)
from app.services.issues import Issue
from app.services.projection import Projector
from app.services.terrain import STEEP_FOOTPRINT_RELIEF_M, TerrainGrid

if TYPE_CHECKING:
    from app.services.osm_parse import BuildingFeature

__all__ = [
    "BuildCancelled",
    "BuildResult",
    "BuildingRecord",
    "Issue",
    "PreparedBuilding",
    "PreparedBuildings",
    "ShellReport",
    "build_meshes",
    "clean_polygons",
    "detect_overlaps",
    "extrude_building",
    "extrude_prepared",
    "prepare_buildings",
    "project_geometry",
    "project_polygon",
]


@dataclass(frozen=True)
class ShellReport:
    """One edge census of a shell: closure, winding, volume, and degeneracy."""

    watertight: bool
    winding_consistent: bool
    volume: float
    degenerate_faces: int
    finite: bool

    @property
    def needs_repair(self) -> bool:
        """True when the generic repair pass has something to fix."""

        return (
            self.degenerate_faces > 0
            or not self.watertight
            or not self.winding_consistent
            or self.volume <= 0.0
        )

    @property
    def valid(self) -> bool:
        """The per-shell acceptance test used before a shell enters the union."""

        return self.finite and not self.needs_repair


def _shell_report(mesh: trimesh.Trimesh) -> ShellReport:
    """Measure a shell in one pass instead of through trimesh's cached properties.

    ``is_watertight``, ``is_winding_consistent``, ``volume``, and the
    degenerate-face mask are four separate edge censuses (and volume runs the
    full mass-properties integral); at ~1 700 shells that costs more than the
    extrusions themselves. One sorted-edge pass yields all of it: every
    undirected edge exactly twice for closure, every directed edge once for
    winding, volume from the divergence theorem, and the same degenerate-face
    rule trimesh uses (a triangle OBB side below ``tol.merge``).

    Winding is reported only for a closed shell; trimesh keeps its flag on an
    open patch, but an open patch is not a shell this pipeline accepts.
    """

    faces = np.asarray(mesh.faces)
    if not len(faces):
        return ShellReport(False, False, 0.0, 0, True)
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    triangles = vertices[faces]
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    double_area = np.sqrt(np.einsum("ij,ij->i", cross, cross))
    volume = float(np.einsum("ij,ij->i", triangles[:, 0], cross).sum() / 6.0)

    count = len(vertices)
    edges = faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    low = np.minimum(edges[:, 0], edges[:, 1]).astype(np.int64)
    high = np.maximum(edges[:, 0], edges[:, 1]).astype(np.int64)
    keys = np.sort(low * count + high)
    if keys.size:
        starts = np.concatenate(([0], np.flatnonzero(np.diff(keys)) + 1))
        counts = np.diff(np.concatenate((starts, [keys.size])))
        watertight = bool((counts == 2).all())
    else:
        watertight = False
    directed = np.sort(edges[:, 0].astype(np.int64) * count + edges[:, 1])
    winding = bool(watertight and (np.diff(directed) > 0).all())

    sides = triangle_extents(triangles=triangles, areas=double_area * 0.5)
    degenerate = int((~(sides > TRIMESH_TOL.merge).all(axis=1)).sum())
    return ShellReport(
        watertight=watertight,
        winding_consistent=winding,
        volume=volume,
        degenerate_faces=degenerate,
        finite=bool(np.isfinite(vertices).all()),
    )


def _extrude_polygon(polygon: Polygon, height: float) -> trimesh.Trimesh:
    """Extrude a polygon into a closed shell that reuses its ring vertices.

    ``trimesh.creation.extrude_polygon`` builds the caps and then a separate wall
    vertex set, so it needs a full vertex merge to become watertight. Building
    the walls from the cap indexes yields exactly the same triangles (pinned by
    ``test_extruded_shell_matches_trimesh_polygon_construction``) with no merge
    pass and no duplicate vertices.
    """

    vertices2d, faces = triangulate_polygon(polygon)
    vertices2d = np.asanyarray(vertices2d, dtype=np.float64)
    faces = np.asanyarray(faces, dtype=np.int64)
    if not len(faces):
        raise ValueError("triangulation produced an empty mesh")

    # Same winding decision as trimesh.creation.extrude_triangulation: the cap
    # triangles must face along +Z for a positive extrusion.
    first = faces[:10]
    edge_a = vertices2d[first[:, 0]] - vertices2d[first[:, 1]]
    edge_b = vertices2d[first[:, 1]] - vertices2d[first[:, 2]]
    signs = edge_a[:, 0] * edge_b[:, 1] - edge_a[:, 1] * edge_b[:, 0]
    if len(signs) and np.sign(signs.mean()) != np.sign(height):
        faces = np.fliplr(faces)

    directed = faces_to_edges(faces)
    boundary = directed[grouping.group_rows(np.sort(directed, axis=1), require_count=1)]
    count = len(vertices2d)
    bottom = np.column_stack([vertices2d, np.zeros(count)])
    top = bottom + np.array([0.0, 0.0, float(height)])
    vertices = np.vstack([bottom, top])

    wall_faces = np.empty((len(boundary) * 2, 3), dtype=np.int64)
    wall_faces[0::2] = np.column_stack(
        [boundary[:, 1] + count, boundary[:, 0] + count, boundary[:, 1]]
    )
    wall_faces[1::2] = np.column_stack([boundary[:, 1], boundary[:, 0] + count, boundary[:, 0]])
    faces_all = np.vstack([np.fliplr(faces), faces + count, wall_faces])
    return trimesh.Trimesh(vertices=vertices, faces=faces_all, process=False)


def _repair_shell(mesh: trimesh.Trimesh) -> None:
    """The legacy repair pass, only for a shell the fast path cannot accept."""

    mesh.update_faces(mesh.nondegenerate_faces())
    if hasattr(mesh, "remove_unreferenced_vertices"):
        mesh.remove_unreferenced_vertices()
    trimesh.repair.fix_winding(mesh)
    trimesh.repair.fix_normals(mesh)


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
    ground_m: float = 0.0
    footprint_relief_m: float = 0.0
    height_rule: str = ""


@dataclass
class PreparedBuilding:
    osm_type: str
    osm_id: int
    tags: dict[str, str]
    source_geometry: Polygon | MultiPolygon
    polygon_local: Polygon
    height_m: float
    base_m: float
    height_source: str
    area_m2: float
    height_rule: str = ""


@dataclass
class PreparedBuildings:
    items: list[PreparedBuilding] = field(default_factory=list)
    bounds: tuple[float, float, float, float] | None = None
    skipped: int = 0
    # Buildings dropped by the density cap (see ``_thin_dense_cells``). They are
    # not "skipped": nothing was wrong with them, the cell simply held too many.
    thinned: int = 0
    height_sources: dict[str, int] = field(default_factory=dict)
    height_sampling: dict[str, Any] = field(default_factory=dict)
    warnings: list[Issue] = field(default_factory=list)


def shift_prepared(prepared: PreparedBuildings, dx: float, dy: float) -> None:
    """Translate every prepared footprint (and the aggregate bounds) by (dx, dy)."""

    for item in prepared.items:
        item.polygon_local = translate(item.polygon_local, xoff=dx, yoff=dy)
    if prepared.bounds is not None:
        minx, miny, maxx, maxy = prepared.bounds
        prepared.bounds = (minx + dx, miny + dy, maxx + dx, maxy + dy)


@dataclass
class BuildResult:
    meshes: list[trimesh.Trimesh] = field(default_factory=list)
    records: list[BuildingRecord] = field(default_factory=list)
    bounds: tuple[float, float, float, float] | None = None
    height_sources: dict[str, int] = field(default_factory=dict)
    height_sampling: dict[str, Any] = field(default_factory=dict)
    skipped: int = 0
    thinned: int = 0
    warnings: list[Issue] = field(default_factory=list)
    # Every record is appended only after the shell report accepted its shell,
    # so this is true by construction; recording it keeps the metadata writer
    # from re-running a trimesh closure census over every shell.
    each_shell_watertight: bool = True


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


def project_geometry(
    geometry: Polygon | MultiPolygon, projector: Projector
) -> Polygon | MultiPolygon:
    if isinstance(geometry, Polygon):
        return project_polygon(geometry, projector)
    return MultiPolygon([project_polygon(part, projector) for part in geometry.geoms])


def _extrude_shell(
    polygon: Polygon,
    height_m: float,
    base_m: float = 0.0,
    embed_depth_m: float = 0.5,
    z_offset: float = 0.0,
) -> tuple[trimesh.Trimesh, ShellReport]:
    """Build one closed shell and return it with the report that accepted it.

    The report is measured here only once for a shell that is already closed
    (the common case); the repair pass gets a second measurement because it
    changes the mesh. ``extrude_prepared`` then accepts or skips the shell from
    this report instead of re-censusing it.
    """

    height_above_base = float(height_m) - float(base_m)
    if height_above_base <= 0.0:
        raise ValueError("height must be greater than base")

    extrusion_height = height_above_base
    if base_m <= 0.0 and embed_depth_m > 0.0:
        # Extend the shell below the ground plane so that the building's side
        # walls do not share a coplanar face with the ground patch.
        extrusion_height += float(embed_depth_m)

    mesh = _extrude_polygon(polygon, extrusion_height)
    if mesh is None or len(mesh.faces) == 0:
        raise ValueError("triangulation produced an empty mesh")

    report = _shell_report(mesh)
    if report.needs_repair:
        _repair_shell(mesh)
        report = _shell_report(mesh)

    if base_m > 0.0:
        mesh.apply_translation((0.0, 0.0, float(base_m)))
    elif embed_depth_m > 0.0:
        mesh.apply_translation((0.0, 0.0, -float(embed_depth_m)))
    if z_offset:
        mesh.apply_translation((0.0, 0.0, float(z_offset)))

    return mesh, report


def extrude_building(
    polygon: Polygon,
    height_m: float,
    base_m: float = 0.0,
    embed_depth_m: float = 0.5,
    z_offset: float = 0.0,
) -> trimesh.Trimesh:
    """Extrude a valid local-metric polygon into a closed building shell.

    ``z_offset`` shifts the whole shell vertically, which is how a building is
    placed on the terrain minimum under its footprint.
    """

    mesh, _report = _extrude_shell(
        polygon,
        height_m,
        base_m=base_m,
        embed_depth_m=embed_depth_m,
        z_offset=z_offset,
    )
    return mesh


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


def _footprint_area_m2(geometry: Any) -> float:
    """Approximate a WGS84 footprint's area in m², to band heights by building size."""

    try:
        area_deg2 = float(geometry.area)
        latitude = float(geometry.centroid.y)
    except Exception:
        return 0.0
    if not math.isfinite(area_deg2) or area_deg2 <= 0.0:
        return 0.0
    metres_per_degree_sq = 111_320.0 * 110_900.0 * max(math.cos(math.radians(latitude)), 0.01)
    return area_deg2 * metres_per_degree_sq


def _override_height(
    overrides: Mapping[str, float] | None,
    feature: BuildingFeature,
) -> float | None:
    """An explicit height for one OSM object, keyed ``"123"`` or ``"way/123"``."""

    if not overrides:
        return None
    for key in (f"{feature.osm_type}/{feature.osm_id}", str(feature.osm_id)):
        value = overrides.get(key)
        if value is not None:
            return float(value)
    return None


def _known_height_pairs(
    features: Sequence[BuildingFeature],
    default_height_m: float,
    level_height_m: float,
    *,
    with_areas: bool = True,
) -> list[KnownHeight]:
    """Height, footprint area, and building type for every tagged building.

    ``with_areas`` is off when sampling is disabled: the pool tail counts still
    come from the tagged heights, and the footprint areas are only needed to
    condition a draw.
    """

    pairs: list[KnownHeight] = []
    for feature in features:
        info = resolve_height(
            feature.tags,
            default_height_m=default_height_m,
            level_height_m=level_height_m,
            footprint_area_m2=_footprint_area_m2(feature.geometry),
        )
        # A clamped height was censored by the accepted range (e.g. height=350
        # becomes 300): it is not an observation worth copying onto other
        # buildings.
        if info.source != "default" and not info.clamped:
            pairs.append(
                KnownHeight(
                    height_m=info.height_m,
                    area_m2=_footprint_area_m2(feature.geometry) if with_areas else 0.0,
                    building_type=feature.tags.get("building", ""),
                )
            )
    return pairs


def _footprint_width(polygons: Sequence[Polygon]) -> float:
    """Narrow side of the largest part's minimum rotated rectangle, in metres.

    The height sampler caps a sampled height at a multiple of this. Using the
    largest part keeps a small annex from setting the width for the whole
    building; a diagonal slab gets its true narrow side, not a bounding box.
    """

    if not polygons:
        return 0.0
    main = max(polygons, key=lambda polygon: polygon.area)
    try:
        rect = main.minimum_rotated_rectangle
    except Exception:  # noqa: BLE001 - a broken ring must not fail the job
        return 0.0
    if rect.is_empty or rect.geom_type != "Polygon":
        return 0.0
    corners = list(rect.exterior.coords)[:4]
    if len(corners) < 2:
        return 0.0
    sides = [
        math.dist(corners[index], corners[(index + 1) % len(corners)])
        for index in range(len(corners))
    ]
    return min(sides) if sides else 0.0


def _sampling_metadata(
    known: Sequence[KnownHeight],
    *,
    jitter: float,
    seed: int,
    sampler: HeightSampler | None,
    conservative: bool,
    slenderness_k: float,
    reason: str,
) -> dict[str, Any]:
    """The ``buildings.height_sampling`` block, with one key set in every mode.

    Sampling can run, be unavailable (too few tagged heights), or be switched
    off. A consumer branches on ``enabled`` and ``reason``, never on which keys
    exist, so the disabled modes carry neutral values: zero counters, empty
    maps, and the configured parameters.
    """

    enabled = sampler is not None
    return {
        "enabled": enabled,
        "reason": "" if enabled else reason,
        "known": len(known),
        "jitter": float(jitter),
        "seed": int(seed),
        "weighting": "type_and_area_similarity" if enabled else "none",
        "type_factors": {"same_type": TYPE_WEIGHT, "type_group": GROUP_WEIGHT},
        "area_bandwidth_log": AREA_BANDWIDTH_LOG,
        "area_ratio": [AREA_RATIO_MIN, AREA_RATIO_MAX],
        "area_weighted": bool(enabled and sampler is not None and sampler.area_weighted),
        "conservative": bool(conservative),
        "slenderness_k": float(slenderness_k),
        "slenderness_width_k": float(WIDTH_SLENDERNESS_K),
        "pool_above_100m": sum(1 for item in known if item.height_m >= 100.0),
        "pool_above_250m": sum(1 for item in known if item.height_m >= 250.0),
        "sampled": 0,
        "sampled_buildings": 0,
        "rules": {},
        "type_capped": 0,
        "band_fallback_draws": 0,
        "sampled_above_100m": 0,
        "sampled_above_250m": 0,
        "above_100m_by_group": {},
    }


def _sampled_tail(
    items: Sequence[PreparedBuilding],
) -> tuple[int, int, int, dict[str, int]]:
    """Sampled buildings (one per OSM id) and their ≥100 m/≥250 m counts.

    A multi-polygon building is prepared once per part but drawn once, so the
    tail is aggregated by ``(osm_type, osm_id)``: counting parts would inflate
    the tall share and let a part-heavy city flip the acceptance check.
    """

    buildings: dict[tuple[str, int], tuple[float, str]] = {}
    for item in items:
        if item.height_source != "sampled":
            continue
        buildings.setdefault(
            (item.osm_type, item.osm_id),
            (item.height_m, item.tags.get("building", "")),
        )
    above_100 = sum(1 for height, _building in buildings.values() if height >= 100.0)
    above_250 = sum(1 for height, _building in buildings.values() if height >= 250.0)
    counts: Counter[str] = Counter()
    present: set[str] = set()
    for height, building in buildings.values():
        group = building_type_group(building)
        present.add(group)
        if height >= 100.0:
            counts[group] += 1
    return (
        len(buildings),
        above_100,
        above_250,
        {group: counts.get(group, 0) for group in sorted(present)},
    )


def prepare_buildings(
    features: Sequence[BuildingFeature],
    projector: object,
    *,
    default_height_m: float = 25.0,
    level_height_m: float = 3.0,
    simplify_m: float = 0.05,
    min_building_area_m2: float = 4.0,
    sample_missing_heights: bool = True,
    height_jitter: float = 0.1,
    height_seed: int = 0,
    min_height_samples: int = 5,
    height_conservative: bool = False,
    height_slenderness_k: float = 8.0,
    max_height_m: float = 300.0,
    min_height_m: float = 2.0,
    height_overrides: Mapping[str, float] | None = None,
    density_cell_m: float = 0.0,
    density_cell_max_buildings: int = 0,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> PreparedBuildings:
    """Project, clean, and resolve heights for every building feature.

    When the selection has enough tagged heights, untagged buildings draw from
    that local distribution (with jitter) instead of all getting the same
    default. A city block and a small town therefore end up with different, but
    locally plausible, skylines.

    ``density_cell_m`` + ``density_cell_max_buildings`` cap the obstacle count
    per grid cell: a cell holding more than the cap keeps only its heaviest
    buildings. ``0`` in either one disables the cap.
    """

    result = PreparedBuildings()
    counts: Counter[str] = Counter()
    total = len(features)

    known = _known_height_pairs(
        features,
        default_height_m,
        level_height_m,
        with_areas=sample_missing_heights,
    )
    sampler: HeightSampler | None = None
    reason = "disabled"
    if sample_missing_heights:
        if len(known) >= max(1, int(min_height_samples)):
            sampler = HeightSampler(
                known,
                jitter=height_jitter,
                seed=height_seed,
                conservative=height_conservative,
                slenderness_k=height_slenderness_k,
            )
            reason = ""
        else:
            reason = "too_few_known_heights"
    result.height_sampling = _sampling_metadata(
        known,
        jitter=height_jitter,
        seed=height_seed,
        sampler=sampler,
        conservative=height_conservative,
        slenderness_k=height_slenderness_k,
        reason=reason,
    )

    rules: Counter[str] = Counter()
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
            max_height_m=max_height_m,
            footprint_area_m2=math.fsum(polygon.area for polygon in polygons),
            min_height_m=min_height_m,
        )
        if height.levels_ignored:
            counts["LEVELS_ONE_IGNORED"] += 1
        override = _override_height(height_overrides, feature)
        if override is not None:
            height = HeightInfo(
                height_m=min(float(override), float(max_height_m)),
                base_m=height.base_m,
                source="override",
            )
        height_rule = ""
        if sampler is not None and height.source == "default":
            footprint_area_m2 = math.fsum(polygon.area for polygon in polygons)
            sampled = sampler.draw(
                footprint_area_m2,
                feature.tags.get("building"),
                width_m=_footprint_width(polygons),
            )
            sampled_height = sampled.height_m
            height_rule = sampled.rule
            # A tagged min_height/building:min_level lifts the base; the draw is
            # a total height, so it cannot put the roof below that base (and one
            # storey above it is the thinnest shell that still makes sense).
            if height.base_m > 0.0:
                floor = min(300.0, height.base_m + float(level_height_m))
                if sampled_height < floor:
                    sampled_height = floor
                    height_rule = "base_floor"
            if sampled_height < float(min_height_m):
                sampled_height = float(min_height_m)
                height_rule = "min_floor"
            rules[height_rule] += 1
            height = HeightInfo(
                height_m=sampled_height,
                base_m=height.base_m,
                source="sampled",
                clamped=height.clamped,
            )
        for polygon in polygons:
            result.bounds = _update_bounds(result.bounds, polygon)
            result.items.append(
                PreparedBuilding(
                    osm_type=feature.osm_type,
                    osm_id=feature.osm_id,
                    tags=dict(feature.tags),
                    source_geometry=feature.geometry,
                    polygon_local=polygon,
                    height_m=height.height_m,
                    base_m=height.base_m,
                    height_source=height.source,
                    area_m2=float(polygon.area),
                    height_rule=height_rule,
                )
            )
            result.height_sources[height.source] = result.height_sources.get(height.source, 0) + 1
            if height.clamped:
                counts["HEIGHT_CLAMPED"] += 1

        if progress_cb is not None:
            progress_cb(index + 1, total)

    if counts["INVALID_GEOMETRY"]:
        result.warnings.append(
            Issue(
                code="INVALID_GEOMETRY",
                message=f"{counts['INVALID_GEOMETRY']} 个建筑轮廓无效或面积过小，已跳过",
                count=counts["INVALID_GEOMETRY"],
            )
        )
    if result.height_sources.get("default") or result.height_sources.get("sampled"):
        missing = result.height_sources.get("default", 0) + result.height_sources.get("sampled", 0)
        if result.height_sources.get("sampled") and sampler is not None:
            message = (
                f"{result.height_sources['sampled']} 栋建筑缺少高度标签，"
                f"已按区域内 {len(sampler)} 栋已知高度随机取值"
                f"（±{float(height_jitter):.0%}）"
            )
        else:
            message = f"{missing} 栋建筑缺少高度标签，已使用默认高度 {default_height_m:g} m"
        result.warnings.append(Issue(code="MISSING_HEIGHT", message=message, count=missing))
    result.height_sampling["sampled"] = result.height_sources.get("sampled", 0)
    result.height_sampling["rules"] = dict(sorted(rules.items()))
    if sampler is not None:
        sampled_buildings, above_100, above_250, by_group = _sampled_tail(result.items)
        result.height_sampling["sampled_buildings"] = sampled_buildings
        result.height_sampling["band_fallback_draws"] = sampler.band_fallbacks
        result.height_sampling["type_capped"] = rules.get("type_cap", 0)
        result.height_sampling["sampled_above_100m"] = above_100
        result.height_sampling["sampled_above_250m"] = above_250
        result.height_sampling["above_100m_by_group"] = by_group
    if counts["HEIGHT_CLAMPED"]:
        result.warnings.append(
            Issue(
                code="HEIGHT_CLAMPED",
                message=f"{counts['HEIGHT_CLAMPED']} 栋建筑高度超出允许范围（低于下限或高于上限），已截断",
                count=counts["HEIGHT_CLAMPED"],
            )
        )
    if counts["LEVELS_ONE_IGNORED"]:
        result.warnings.append(
            Issue(
                code="LEVELS_ONE_IGNORED",
                message=(
                    f"{counts['LEVELS_ONE_IGNORED']} 栋建筑的 building:levels=1 与轮廓面积"
                    "不符（>300 m2），已按缺失高度处理并重新抽样"
                ),
                count=counts["LEVELS_ONE_IGNORED"],
            )
        )
    thinned = _thin_dense_cells(
        result,
        cell_m=float(density_cell_m),
        max_per_cell=int(density_cell_max_buildings),
    )
    if thinned:
        result.warnings.append(
            Issue(
                code="DENSITY_THINNED",
                message=(
                    f"{thinned} 栋建筑处于过密区域（{float(density_cell_m):g} m 网格内多于 "
                    f"{int(density_cell_max_buildings)} 栋），已按体量优先只保留前 "
                    f"{int(density_cell_max_buildings)} 栋"
                ),
                count=thinned,
            )
        )
    floating = _ground_unsupported_bases(result)
    if floating:
        result.warnings.append(
            Issue(
                code="FLOATING_BASE",
                message=(
                    f"{floating} 栋建筑的 min_height 下方没有能接住它的楼体，已落到地面"
                    "（否则会在实体里留下一个悬空壳体）"
                ),
                count=floating,
            )
        )
    return result


def _density_rank(item: PreparedBuilding) -> tuple[float, float, float, str, int]:
    """Survival order in an over-dense cell: heaviest first, then tallest.

    The cap deletes; it must not decide which of 60 huts per hectare is a tower.
    Ranking by mass (area x height) keeps the blocks that carry the obstacle
    field and drops the sheds, and the id tail makes the choice deterministic.
    """

    return (
        -float(item.area_m2 * item.height_m),
        -float(item.height_m),
        -float(item.area_m2),
        str(item.osm_type),
        int(item.osm_id),
    )


def _thin_dense_cells(
    result: PreparedBuildings,
    cell_m: float,
    max_per_cell: int,
) -> int:
    """Drop buildings from every grid cell that holds more than ``max_per_cell``.

    Density is the thing being fixed, so the extras are deleted rather than
    enlarged: a district with 60 sheds per hectare is neither resolvable at a
    4 km modelling scale nor worth 60 shells, while the survivors keep their real
    footprint and height. ``bounds`` is deliberately left alone — the ground
    slab still covers the whole selection, so a thinned cell becomes flat ground
    instead of a hole in the base.
    """

    if cell_m <= 0.0 or max_per_cell <= 0 or not result.items:
        return 0

    groups: dict[tuple[int, int], list[int]] = {}
    for index, item in enumerate(result.items):
        point = item.polygon_local.centroid
        key = (int(math.floor(point.x / cell_m)), int(math.floor(point.y / cell_m)))
        groups.setdefault(key, []).append(index)

    keep: set[int] = set()
    removed_sources: Counter[str] = Counter()
    for indices in groups.values():
        if len(indices) <= max_per_cell:
            keep.update(indices)
            continue
        ranked = sorted(indices, key=lambda index: _density_rank(result.items[index]))
        keep.update(ranked[:max_per_cell])
        removed_sources.update(result.items[index].height_source for index in ranked[max_per_cell:])

    removed = len(result.items) - len(keep)
    if removed <= 0:
        return 0
    result.items = [item for index, item in enumerate(result.items) if index in keep]
    for source, count in removed_sources.items():
        result.height_sources[source] = max(0, result.height_sources.get(source, 0) - count)
    result.thinned += removed
    return removed


def _ground_unsupported_bases(result: PreparedBuildings) -> int:
    """Lower the base of every elevated building that nothing holds up.

    A ``min_height`` building is extruded from its base upwards, which assumes
    another volume fills the space below — usually the podium mapped as its own
    building. When that volume is missing (or is itself elevated and unsupported)
    the shell floats, the fused city is no longer one solid, and a CFD solver
    sees a detached box. Those buildings are dropped to the ground instead.
    """

    items = result.items
    elevated = [index for index, item in enumerate(items) if item.base_m > 0.0]
    if not elevated:
        return 0

    tree = STRtree([item.polygon_local for item in items])
    supported = {index for index, item in enumerate(items) if item.base_m <= 0.0}
    pending = [index for index in elevated if index not in supported]
    while pending:
        progressed = False
        still: list[int] = []
        for index in pending:
            item = items[index]
            candidates = tree.query(item.polygon_local, predicate="intersects")
            if any(
                int(other) in supported and items[int(other)].height_m >= item.base_m
                for other in candidates
                if int(other) != index
            ):
                supported.add(index)
                progressed = True
            else:
                still.append(index)
        pending = still if progressed else []
        if not progressed:
            break

    floating = [index for index in elevated if index not in supported]
    for index in floating:
        items[index].base_m = 0.0
    return len(floating)


def _contact_indices(polygons: Sequence[Polygon], tolerance: float) -> set[int]:
    """Indexes of footprints that touch (or nearly touch) another footprint."""

    if tolerance <= 0.0 or len(polygons) < 2:
        return set()
    tree = STRtree(list(polygons))
    # One vectorised distance query replaces a buffer per footprint plus a
    # candidate scan each.
    left, right = tree.query(list(polygons), predicate="dwithin", distance=float(tolerance))
    touching: set[int] = set()
    for first, second in zip(left.tolist(), right.tolist(), strict=True):
        if first >= second:
            continue
        touching.add(int(first))
        touching.add(int(second))
    return touching


def extrude_prepared(
    prepared: PreparedBuildings,
    *,
    terrain: TerrainGrid | None = None,
    embed_depth_m: float = 0.5,
    contact_overlap_m: float = 0.1,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> BuildResult:
    """Place every prepared footprint on the terrain and build closed shells.

    Footprints that touch a neighbour are grown by ``contact_overlap_m / 2`` so
    their shells share real volume. A union of edge- or corner-touching shells
    has no common interior and is not manifold, which used to be patched with
    0.4 m weld boxes; a 0.1 m overlap reaches the same closed surface with a far
    smaller artefact and no extra repair pass.
    """

    result = BuildResult(
        bounds=prepared.bounds,
        height_sources=dict(prepared.height_sources),
        height_sampling=dict(prepared.height_sampling),
        skipped=prepared.skipped,
        thinned=prepared.thinned,
        warnings=list(prepared.warnings),
    )
    counts: Counter[str] = Counter()
    total = len(prepared.items)

    footprints = [item.polygon_local for item in prepared.items]
    grow = max(0.0, float(contact_overlap_m)) / 2.0
    expanded: dict[int, Polygon] = {}
    if grow > 0.0:
        for index in _contact_indices(footprints, max(0.0, float(contact_overlap_m))):
            buffered = footprints[index].buffer(grow, join_style=2, mitre_limit=2.0)
            if buffered.is_valid and not buffered.is_empty:
                expanded[index] = buffered

    for index, item in enumerate(prepared.items):
        if cancel_event is not None and cancel_event.is_set():
            raise BuildCancelled("job cancelled")

        ground = float(terrain.min_over(item.polygon_local)) if terrain is not None else 0.0
        relief = float(terrain.relief_over(item.polygon_local)) if terrain is not None else 0.0
        mesh = None
        report: ShellReport | None = None
        # The grown footprint can occasionally trip the triangulator; fall back to
        # the exact footprint rather than dropping the building.
        for candidate in (expanded.get(index), item.polygon_local):
            if candidate is None:
                continue
            try:
                mesh, report = _extrude_shell(
                    candidate,
                    height_m=item.height_m,
                    base_m=item.base_m,
                    embed_depth_m=embed_depth_m,
                    z_offset=ground,
                )
                break
            except Exception:
                continue
        if mesh is None or report is None:
            counts["TRIANGULATION_FAILED"] += 1
            result.skipped += 1
            continue

        if not report.valid:
            counts["INVALID_MESH"] += 1
            result.skipped += 1
            continue

        if terrain is not None and relief > STEEP_FOOTPRINT_RELIEF_M:
            counts["STEEP_FOOTPRINT"] += 1

        result.meshes.append(mesh)
        result.records.append(
            BuildingRecord(
                osm_type=item.osm_type,
                osm_id=item.osm_id,
                tags=item.tags,
                source_geometry=item.source_geometry,
                polygon_local=item.polygon_local,
                height_m=item.height_m,
                base_m=item.base_m,
                height_source=item.height_source,
                area_m2=item.area_m2,
                mesh=mesh,
                ground_m=ground,
                footprint_relief_m=relief,
                height_rule=item.height_rule,
            )
        )

        if progress_cb is not None:
            progress_cb(index + 1, total)

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
    if counts["STEEP_FOOTPRINT"]:
        result.warnings.append(
            Issue(
                code="STEEP_FOOTPRINT",
                message=(
                    f"{counts['STEEP_FOOTPRINT']} 栋建筑轮廓范围内地形高差超过 "
                    f"{STEEP_FOOTPRINT_RELIEF_M:g} m，坡地会切掉部分楼体"
                ),
                count=counts["STEEP_FOOTPRINT"],
            )
        )
    return result


def build_meshes(
    features: Sequence[BuildingFeature],
    projector: object,
    *,
    default_height_m: float = 25.0,
    level_height_m: float = 3.0,
    embed_depth_m: float = 0.5,
    contact_overlap_m: float = 0.1,
    simplify_m: float = 0.05,
    min_building_area_m2: float = 4.0,
    sample_missing_heights: bool = True,
    height_jitter: float = 0.1,
    height_seed: int = 0,
    min_height_samples: int = 5,
    height_conservative: bool = False,
    height_slenderness_k: float = 8.0,
    max_height_m: float = 300.0,
    height_overrides: Mapping[str, float] | None = None,
    density_cell_m: float = 0.0,
    density_cell_max_buildings: int = 0,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> BuildResult:
    """Prepare and extrude on flat ground (single-pass compatibility wrapper)."""

    prepared = prepare_buildings(
        features,
        projector,
        default_height_m=default_height_m,
        level_height_m=level_height_m,
        simplify_m=simplify_m,
        min_building_area_m2=min_building_area_m2,
        sample_missing_heights=sample_missing_heights,
        height_jitter=height_jitter,
        height_seed=height_seed,
        min_height_samples=min_height_samples,
        height_conservative=height_conservative,
        height_slenderness_k=height_slenderness_k,
        max_height_m=max_height_m,
        height_overrides=height_overrides,
        density_cell_m=density_cell_m,
        density_cell_max_buildings=density_cell_max_buildings,
        cancel_event=cancel_event,
    )
    return extrude_prepared(
        prepared,
        terrain=None,
        embed_depth_m=embed_depth_m,
        contact_overlap_m=contact_overlap_m,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
    )
