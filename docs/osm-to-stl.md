# From OSM footprints to CFD STL

This note explains the geometry and the assumptions behind the generated
files. It is aimed at users who will import the STL into OpenFOAM, STAR-CCM+,
Fluent, or another outdoor wind solver.

## 1. Source data

The Overpass query requests:

```overpassql
[out:json][timeout:180];
(
  way["building"](south,west,north,east);
  relation["building"](south,west,north,east);
);
out body;
>;
out skel qt;
```

A `way` with a `building` tag is a footprint. A `relation` with a `building` tag
is usually a multipolygon (courtyard, apartment block, campus building). The
parser keeps polygon geometries and skips relation member ways so the same
footprint is not emitted twice. `building=no`, `construction`, and `proposed`
are ignored.

A selection bigger than `overpass_chunk_area_km2` (4 km² by default) runs the
same query once per grid cell of at most 2 km per side; the results are merged
and duplicate elements (ways crossing a cell border) are dropped. The merged
extent and geometry are the same as one big query, but no single request is
large enough to time out on a busy public server. If one cell still fails after
its endpoint retries, it is retried as four sub-cells instead of failing the
job: the recursion stops after `overpass_split_max_depth` levels (2) and never
goes below `overpass_split_min_area_km2` (0.25 km²). Every sub-cell is cached
under its own bbox, so a later run reads the same data from disk.
`metadata.json → source.overpass_splits` records where this run had to split
and `source.overpass_cell_times` how long each cell it fetched took.

Elevations are a second external input: AWS Terrain Tiles (Terrarium), decoded
from 8-bit RGB PNG as `m = R*256 + G + B/256 - 32768`, resampled onto a local
grid and median-smoothed. See section 6.

OSM footprints are 2D and often approximate. They do not contain roof shape or
terrain. The generated model is therefore a set of vertical prisms on a DEM
surface, which is the usual first-order geometry for urban wind CFD.

## 2. Projection and local frame

Let the selected WGS84 bounding box be
`(south, west, north, east)`. The UTM zone is computed from the bbox centre:

```text
zone = floor((lon_center + 180) / 6) + 1
EPSG = 32600 + zone   (north)
     = 32700 + zone   (south)
```

Each footprint ring is transformed `WGS84 → UTM`, then shifted by the UTM
coordinates of the bbox south-west corner:

```text
x_local = easting  - easting_origin
y_local = northing - northing_origin
z_local = height above the local datum (z=0)
```

The result is a right-handed frame:

- `+X` east,
- `+Y` north,
- `+Z` up,
- 1 unit = 1 metre.

`z = 0` is the lowest terrain cell of the selection, or the flat ground plane
when terrain is disabled. `metadata.json → terrain.datum_offset_m` converts back
to absolute DEM elevations.

The STL is written in this Z-up frame. The Three.js preview rotates it to Y-up
only for display. `metadata.json` stores the origin's easting/northing and
longitude/latitude, so a point can be mapped back:

```text
easting  = x_local + origin.easting
northing = y_local + origin.northing
lon, lat = UTM_to_WGS84(easting, northing)
```

## 3. Height resolution

For each footprint, height is resolved in this order:

1. **`height` tag.** Examples: `12`, `12 m`, `12.5m`, `40'` (feet). The parser
   extracts the first number and converts feet to metres.
2. **Levels.** `building:levels × level_height`, plus `roof:height` if present.
3. **Sampled.** If at least `height_sampling_min_samples` buildings in the
   selection carry a height or level tag, an untagged building draws from those
   observations with ±`height_sampling_jitter` (10 % by default) jitter and a
   deterministic seed. The pool is conditioned on the target: only observations
   whose footprint area is within 0.5–2× the target's are considered (the draw
   falls back to the weighted pool of every observation with a known area when
   that band holds none), and each candidate
   is weighted by `exp(-0.5 × (ln(area / target_area) / 0.5)²)`, times 2 when
   the `building` type matches and 1.5 when only the type group matches
   (residential / commercial / civic / industrial / other). OSM height tags
   over-represent towers, so the band keeps a 112 m² building from copying a
   239 m tower while the weighting keeps a small shed from winning a draw for a
   large footprint. Heights that the 2–300 m range had to clamp (for example an
   OSM `height=350`) are not observations and are skipped. Low-rise, civic, and
   industrial `building` types get a sampled ceiling (train station 60 m, school
   35 m, museum 45 m, retail 30 m, warehouse 25 m, house 15 m, …). Every sampled
   building records the rule that produced its height (`similarity`, `type_cap`,
   or `conservative_cap`) as `height_rule` in `buildings.geojson`.
   `metadata.json → buildings.height_sampling` records the pool size, the
   weights, the rule counts (`sampled`, `sampled_buildings`, `rules`), the
   band-fallback draws, and how many sampled buildings and pool observations are
   above 100 m and 250 m, by type group. `sampled` counts prepared polygon parts
   (a multi-polygon building once per part), `sampled_buildings` counts distinct
   OSM buildings, and the tail counts are per building. `type_capped` counts
   draws whose final rule is `type_cap`; a draw that the conservative bound
   lowers again reports `conservative_cap`.

   **Conservative mode.** `--conservative-heights`
   (`CITYGEN_HEIGHT_SAMPLING_CONSERVATIVE=true`) also caps every sampled height
   at `k × sqrt(footprint area)`, `k = 8` by default (`--slenderness-k`,
   `CITYGEN_HEIGHT_SAMPLING_SLENDERNESS_K`). It never raises a height; it only
   trims the slenderness tail for users who want a calmer skyline.
4. **Default.** `default_height_m` (25 m) when too few tagged heights exist.

To check a finished job, run
`python -m app.services.height_report --output <job dir> [--check]`: it prints
the sampled tail against the tagged pool, the rule counts, and the most slender
samples, and `--check` fails when the sampled `>=250 m` share exceeds the pool's,
a sampled height exceeds its type ceiling, or a conservative run exceeds
`k × sqrt(area)`. A job from a version before this one has no pool tail in its
metadata, so the report skips the share check and says so.

`min_height` raises the base of the prism. If a height is outside the accepted
range it is clamped and a `HEIGHT_CLAMPED` warning is emitted. A missing height
produces `MISSING_HEIGHT`; this is the most common warning in OSM data and
should be reviewed before a production CFD run.

The extrusion height is:

```text
h_extrude = max(height - base, 0) + embed_depth   if base <= 0
h_extrude = max(height - base, 0)                 if base > 0
```

The `embed_depth` (0.5 m by default) extends the shell below the local ground so
the building side walls do not share a coplanar face with the terrain surface.
This avoids a common meshing failure at the wall/ground intersection.

With terrain, the "local ground" is `min_over(footprint)`: the lowest sampled
terrain height under the footprint. The whole shell is translated there, so the
base sinks into the hill and the union cuts the building at the real surface.
A footprint spanning more than 3 m of relief gets a `STEEP_FOOTPRINT` warning.

## 4. Footprint cleanup

Raw OSM polygons can be invalid (self-intersecting rings, duplicate nodes,
zero-area spikes). The pipeline:

1. applies `make_valid` (fallback `buffer(0)`);
2. extracts Polygon/MultiPolygon parts;
3. orients the exterior ring counter-clockwise and keeps holes;
4. drops holes below 1 m²;
5. optionally simplifies with `preserve_topology=True` (default 0.05 m);
6. drops parts below `min_building_area_m2` (default 4 m²).

A footprint with a courtyard becomes an extruded prism with an inner hole. The
triangulation (mapbox-earcut via trimesh) creates top/bottom caps with holes and
vertical side walls for both the exterior and the hole.

## 5. Closed shell construction

For a polygon with area `A`, perimeter `P`, height `h`, and embed depth `e`, the
closed shell has:

- a bottom cap at `z = -e` (or `base`),
- a top cap at `z = height`,
- side walls along every exterior and interior ring,
- volume approximately `A × (h + e)` for a vertical prism,
- outward normals after `trimesh.repair.fix_winding` / `fix_normals`.

Every shell is checked before it enters the output:

```text
is_watertight
is_winding_consistent
all(isfinite(vertices))
volume > 0
faces > 0
```

A shell that fails is skipped and counted in `INVALID_MESH`. Accepted shells
still enter the city union; `buildings.each_shell_watertight` records that every
input shell passed its individual checks, not that the final city is watertight.

## 6. Ground and city fusion

The ground is always a closed solid of thickness `CITYGEN_GROUND_SLAB_THICKNESS_M`
(1 m by default) from `-thickness` up to the surface. It covers the union of the
selected bbox and all building footprints, expanded by `ground_margin_m`. There
is no open plane mode and no option to omit the ground: the output contract is
one closed city.

With terrain enabled (default), the top of that solid is a DEM height field
sampled on a local grid (30 m by default) and smoothed with a ~150 m running
median, built as a closed block (DEM top, vertical skirt, flat bottom). With
terrain disabled it is a flat slab at `z=0`.

`fuse_city` boolean-unions the building shells and the base with Manifold and
validates the result. If two buildings touch only at an edge or corner, their
volumes have no common interior; the union has an edge shared by four faces and
cannot be a manifold surface. The repair pass:

1. merges vertices and finds every edge with more than two incident faces;
2. clusters nearby bad edges and adds a 0.4 m padded solid box around each
   cluster;
3. re-runs the union;
4. repeats up to eight times if new contacts appear.

The written STL is then loaded again and checked. `metadata.json → city` records
`triangle_count`, `watertight`, `winding_consistent`, `volume_m3`,
`non_manifold_edges`, `base_thickness_m`, `terrain_triangle_count`,
`union_engine`, and `weld_repairs`.
If the final file is not closed, the job fails with `CITY_UNION_FAILED` rather
than writing a concatenated triangle soup.

## 7. Binary STL layout

The writer emits a standard binary STL:

```text
80-byte header
uint32   triangle count
repeat triangle count times:
  float32[3] normal
  float32[3] vertex A
  float32[3] vertex B
  float32[3] vertex C
  uint16     attribute byte count (0)
```

STL is a triangle soup: it has no units, no coordinate-system metadata, and no
per-building groups. That is why the ZIP also contains `metadata.json`,
`buildings.geojson`, and `patches.json` (ground/wall/bottom ranges). Always
import the STL with the metadata's unit and frame; do not assume the solver's
default unit is metres.

## 8. Patch groups

`patches.json` sits next to `city.stl` and maps every triangle to one of three
CFD patches, without changing the geometry:

- `ground` — upward triangles that lie on the terrain: the centroid **and all
  three vertices** are within `CITYGEN_PATCH_GROUND_TOLERANCE_M` (0.05 m by
  default) of the surface, and the face normal agrees with the terrain normal
  (`CITYGEN_PATCH_GROUND_NORMAL_DOT_MIN`, 0.9). With terrain off the reference is
  the flat ground at `z = 0` and its up normal.
- `wall` — building walls, roofs, and the vertical terrain skirt around the
  base border.
- `bottom` — the base underside (every vertex on the mesh's lowest plane).

The file has this shape:

```json
{
  "schema_version": "1.0",
  "unit": "m",
  "triangle_count": 87388,
  "ground_tolerance_m": 0.05,
  "ground_normal_dot_min": 0.9,
  "counts": {"ground": 54518, "wall": 32322, "bottom": 548},
  "ranges": [
    {"start": 0, "count": 11, "kind": "wall"},
    {"start": 11, "count": 3, "kind": "ground"}
  ]
}
```

`ranges` cover `[0, triangle_count)` contiguously without overlap, and the
triangle order is the STL order. A solver that reads triangles sequentially can
therefore assign the boundary patch in the same pass. The classification runs on
the reloaded `city.stl`, so the order is the file's own. The whole-triangle and
normal tests keep a near-surface roof triangle on a slope, and a near-vertical
wall sliver that touches the ground plane, out of `ground`; a triangle that
really lies on the terrain is always `ground`, however steep the slope.

## 9. CFD import checklist

1. **Units.** Confirm the solver reads the STL as metres. A 100 m building
   should measure 100 units, not 100 mm or 100 ft.
2. **Orientation.** `+Z` is up. If the solver uses Y-up, rotate `-90°` about X.
3. **Patch.** Import `city.stl` as one closed obstacle or solid. When the solver
   needs separate ground and wall boundaries, assign them from `patches.json`
   (contiguous ranges in STL order) instead of splitting the geometry; there is
   still exactly one closed surface.
4. **Domain.** Create a fluid domain with an inlet, outlet, sides, and top.
   Recommended initial distances: 5H upstream, 10–15H downstream, 5H sides,
   5H top, where H is the tallest building (tune for your study).
5. **Ground.** The ground is part of the solid; its bottom is a valid closed
   boundary at `-1 m` by default. Without terrain the top is at `z=0`; with
   terrain the surface follows the DEM and `z=0` is the lowest terrain cell.
   Buildings on slopes are cut by the hill.
6. **Contacts.** Check `OVERLAP_DETECTED` and `city.weld_repairs` in the UI or
   `metadata.json`. Touching footprints are already fused; the weld count tells
   you whether local contact repairs were necessary.
7. **Heights.** Check `MISSING_HEIGHT`, `HEIGHT_CLAMPED`, and `STEEP_FOOTPRINT`.
   A default height of 25 m is a placeholder, not a surveyed value.
8. **Wind direction.** The local axes are east/north. Rotate the model or the
   domain to align the inlet with the study's prevailing wind.
9. **Mesh.** This tool stops at surface STL. Volume meshing, boundary layers,
   turbulence models, and solver dictionaries are outside its scope.

## 10. Known limitations

- Terrain is a 30 m grid resampled from ~30 m DEM data and median-smoothed at
  ~150 m: no cliffs, buildings, vegetation, or carved streams.
- No bridges or overhangs; buildings on steep slopes are cut by the hill.
- No pitched/flat roof distinction; every building is a vertical prism.
- No `building:part` or roof detail.
- No roads, trees, or porous vegetation.
- Touching buildings are fused with local weld boxes, not reconstructed as
  shared architectural walls.
- OSM height tags are sparse and sometimes wrong.
- Patch classification tests the whole triangle (centroid and vertices) and its
  normal, so steep terrain no longer hides roofs in `ground`; a three-sided
  sliver that only touches the ground plane stays `wall`.
- Public Overpass endpoints can be slow or rate-limited; the cache helps after
  the first request. DEM tiles are cached the same way.
- `city.stl` is a validated closed surface, but it is not a volume mesh or a
  complete fluid-domain boundary; you still build the outer domain.

These limitations are deliberate: the goal is a transparent, inspectable
geometry pipeline with provenance and warnings, not a black-box city model.
