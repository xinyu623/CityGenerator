# Architecture

## Overview

CityGenerator is a single-process FastAPI application with a disk-backed job
queue. The browser never talks to Overpass directly: it asks the backend to
create a job, then polls the job state. This keeps the OSM query, geometry work,
and large STL files on the server side.

```text
Browser (Leaflet + vanilla ES modules)
   │  POST /api/jobs  (bbox + options)
   ▼
FastAPI API  ──────────────►  JobManager  ──►  ThreadPoolExecutor (2 workers)
   │                              │                    │
   │  GET /api/jobs/{id}          │ job.json           ▼
   └──────────────────────────────┘              run_pipeline
                                                       │
                     ┌─────────────────────────────────┼──────────────────┐
                     ▼                                 ▼                  ▼
               OverpassClient                     geometry           output writer
               (cache + retries)             (projection/extrude)   (STL/ZIP/meta)
```

The frontend is served from `app/static/` at `/`. There is no Node build step:
ES modules are loaded directly, and Leaflet/Three.js come from jsDelivr. The
static files are included in the Python package via `pyproject.toml`.

## Module map

| Module | Responsibility |
| --- | --- |
| `app/main.py` | App factory, lifespan, CORS, static mount, `/` and `/favicon.ico`. |
| `app/config.py` | `Settings` (`CITYGEN_*` env / `.env`), paths, limits, defaults. |
| `app/models/schemas.py` | Pydantic request/response models and validation. |
| `app/api/meta.py` | `/api/health`, `/api/config`. |
| `app/api/jobs.py` | Create, status, download, file, delete endpoints. |
| `app/services/overpass.py` | Overpass query, endpoint fallback, retries, throttle, cache. |
| `app/services/osm_parse.py` | `osm2geojson` conversion, building filtering, relation handling. |
| `app/services/projection.py` | UTM zone selection, `Projector`, local ↔ WGS84. |
| `app/services/heights.py` | Parse `height`, `building:levels`, `min_height`, clamp, and sample missing heights. |
| `app/services/terrain.py` | DEM height grid, exact triangle sampling, footprint minima, closed terrain solid. |
| `app/services/dem.py` | Terrarium tile fetch/decode/cache, zoom choice, mosaicking, reprojection. |
| `app/services/issues.py` | Shared `Issue` (code/message/count) warning record. |
| `app/services/buildings.py` | Clean/project footprints, resolve heights, place on terrain, extrude shells. |
| `app/services/ground.py` | Flat solid ground slab (terrain disabled). |
| `app/services/city.py` | Manifold boolean union, contact weld repair, closed-city validation. |
| `app/services/stl_export.py` | Binary STL writer/reader, validation, non-manifold edge count. |
| `app/services/output.py` | Metadata, GeoJSON, README, ZIP, file inventory. |
| `app/services/pipeline.py` | Orchestrates stages, progress, cancellation, warnings. |
| `app/services/jobs.py` | Disk state, thread pool, cancel/delete, recovery, cleanup. |
| `app/services/errors.py` | Typed domain errors with stable codes. |
| `app/services/io_utils.py` | Atomic JSON/bytes writes, UTC helpers, retention. |
| `app/static/` | Chinese UI: map selection, options, polling, result, 3D viewer. |

## Job lifecycle

1. `POST /api/jobs` validates the Pydantic request and writes
   `data/jobs/<id>/job.json` plus `request.json`.
2. `JobManager` submits `_run` to a `ThreadPoolExecutor` and returns `202`.
3. `_run` updates `job.json` through a throttled progress callback. The
   callback writes at most twice per second per stage; per-building progress
   would otherwise dominate cache-hit latency.
4. `run_pipeline` performs the stages below and returns an `OutputBundle`.
5. On success, `job.json` is updated with `stats`, `warnings`, `files`, and
   `download_url`; the worker removes its in-memory cancel event/future.
6. `GET /api/jobs/{id}` reads `job.json`. `GET .../download` serves
   `result.zip`; `GET .../files/{name}` serves an allow-listed output file.
7. `DELETE` sets the cancel event, waits up to two seconds for the worker, then
   removes the job directory. If the worker is still running, the API returns
   `409` and the client can retry.

A restart marks leftover `queued`/`running` jobs as `failed` with
`SERVER_RESTARTED`, so the UI never polls a zombie forever. Completed jobs and
the Overpass cache are pruned after `CITYGEN_JOB_TTL_DAYS` /
`CITYGEN_CACHE_TTL_DAYS`.

### Progress stages

| Stage | Approx. percent | Work |
| --- | --- | --- |
| `validating` | 0 | Bounds order, area cap, option bounds. |
| `fetching` | 4–22 | Overpass cache lookup or HTTP request. |
| `parsing` | 22–30 | OSM JSON → Shapely footprints. |
| `projecting` | 30–34 | UTM projector + local origin. |
| `building` | 34–40 | Project/clean/resolve heights for every footprint. |
| `terrain` | 35–40 | DEM tile fetch, grid resampling, terrain block. |
| `building` | 40–78 | Place each shell on the terrain and extrude it. |
| `ground` | 80 | Solid terrain block (or 1 m flat slab). |
| `fusing` | 84 | Boolean-union buildings and base; weld edge contacts. |
| `exporting` | 90 | STL validation, metadata, GeoJSON, ZIP. |
| `completed` | 100 | Terminal. |

The exact percentages are advisory; the UI shows the stage label and message.
`building` appears twice because footprints must be projected before the terrain
extent is known, and extruded after the terrain exists.

## Data flow

### 1. Overpass

`OverpassClient` builds a versioned query (`buildings-v1`) over ways and
relations with a `building` tag, then fetches member nodes via `out body; >;
out skel qt;`. It tries configured endpoints in order, retries transient
failures, and enforces a minimum interval between requests.

A selection larger than `overpass_chunk_area_km2` (default `4 km²`) is split
into a grid of cells of at most `sqrt(4) = 2 km` per side
(`OverpassClient.chunk_bboxes`). Each cell is a normal query and is cached on its
own; the raw payloads are merged afterwards, dropping elements that appear in
more than one cell (a way crossing a cell border is returned whole by every cell
that touches it). Every cell keeps the endpoint retry/backoff policy. A cell
that still fails after its retries is retried as four sub-cells, recursively at
most `overpass_split_max_depth` levels deep (2) and never below
`overpass_split_min_area_km2` (0.25 km²); each sub-cell is cached under its own
bbox and the merged payload is cached under the parent cell. A cell that cannot
be subdivided fails the whole fetch — a silently missing cell would drop
buildings. Successful raw JSON is cached at:

```text
data/cache/osm/<sha256(query_version|bbox)>.json
```

for both the individual cells and the merged parent bbox. The cache key
deliberately ignores options such as default height: the raw OSM response is
independent of modelling parameters. `refresh=true` bypasses the cache and
re-fetches every cell. `metadata.json → source.overpass_splits` lists each split
of the current run (bbox, depth, child count, error) and
`source.overpass_cell_times` the elapsed time of every cell the run fetched,
including cache hits; a top-level cache hit bypasses the cells entirely.

### 2. Parsing

`osm2geojson.json2shapes` converts elements to Shapely geometries with
`filter_used_refs=False` so relation member ways can be identified. The parser:

- keeps polygons only;
- skips relation member ways to avoid duplicate footprints;
- skips `building=no`, `construction`, `proposed`;
- attaches selected tags (`height`, `building:levels`, `min_height`, `roof:height`,
  `name`, `building`);
- carries OSM type/id for provenance.

### 3. Projection

The UTM zone is derived from the bbox centre; northern/southern hemisphere
selects `EPSG:326xx` / `EPSG:327xx`. A `Projector` transforms WGS84 → UTM and
subtracts the bbox south-west corner, producing a local metric frame with
`+X=east`, `+Y=north`, `+Z=up`. The origin's UTM and WGS84 coordinates are kept
for metadata.

### 4. Height resolution

Priority:

1. `height` tag (supports `12`, `12 m`, `12.5m`, feet values such as `40'`).
2. `building:levels × level_height + roof:height` (if levels are present).
3. Default height (25 m).

`min_height` raises the base. Heights are clamped to a safe range and produce
`HEIGHT_CLAMPED`; missing heights produce `MISSING_HEIGHT`.

Untagged buildings sample the selection's tagged heights (`HeightSampler` in
`app/services/heights.py`). Candidates must lie within 0.5–2× the target
footprint area (when the band holds none, the draw falls back to the weighted
pool of every observation with a known area) and are weighted by a log-area
Gaussian kernel plus a `building`-type factor: same type ×2, same type group
×1.5. `prepare_buildings` records the rule that produced each height
(`similarity`, `type_cap`, `conservative_cap`), the pool's and the sample's
≥100 m/≥250 m counts (per OSM building, with `sampled` still counting prepared
parts), the band-fallback draws, and the optional conservative bound
(`height ≤ k·sqrt(area)`, `k = 8`, `CITYGEN_HEIGHT_SAMPLING_CONSERVATIVE`).
`python -m app.services.height_report --output <job>` reports the same numbers
from a finished output directory.

### 5. Terrain (DEM)

Terrain is on by default. `app/services/dem.py` implements the AWS Terrarium
source:

- `choose_zoom` picks the shallowest zoom whose pixel is finer than the
  requested resolution, clamped to `dem_zoom_min`/`dem_zoom_max`.
- Tiles are fetched from
  `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png`,
  cached at `data/cache/dem/aws-terrarium-1/<z>/<x>/<y>.png`, retried with
  backoff on 429/5xx, and reused regardless of the requested bbox.
- The 8-bit RGB PNG is decoded with Pillow as
  `m = R*256 + G + B/256 - 32768`; bilinear interpolation inside a tile,
  straight mosaic lookup across tiles.
- The tile set is planned up front and reduced (one zoom level down at a time,
  recording `zoom_reduced`) if it would exceed `dem_max_tiles`.
- Failures raise `TerrainError` → `TERRAIN_FETCH_FAILED`; there is no silent
  flat fallback.

`app/services/terrain.py` turns the sampled values into a `TerrainGrid`:

- Grid spacing comes from `terrain.resolution_m` (default 30 m, allowed 5–50 m),
  coarsened until the cell count fits `max_terrain_cells` (warning
  `TERRAIN_RESOLUTION_REDUCED`).
- Grid nodes are transformed to WGS84 with `Projector.local_many_to_wgs84` and
  sampled at the DEM.
- A margin of `kernel // 2` cells is sampled and cropped away after filtering, so
  the running median never sees replicated edge values.
- The field is smoothed with an exact 2-D running median whose width is fixed in
  ground metres (`DEFAULT_SMOOTHING_M = 150 m`, recorded as `smoothing_m` and
  `filter`). The source is a surface model: without this step a single bad 30 m
  cell becomes a 100 m-wide dome. Median, not mean, so linear slopes survive.
- Nodata pixels are filled from neighbouring nodes (`TERRAIN_NODATA_FILLED`);
  an all-nodata area raises `TerrainDataError` → `TERRAIN_DATA_MISSING`.
- Heights are shifted so the minimum is `0`; the shift is kept as
  `datum_offset_m` for metadata.
- `surface_z(x, y)` reproduces the mesh exactly (each cell is split along
  `v00–v11`), and `min_over(polygon)`/`relief_over(polygon)` use the covering
  nodes plus exact vertex and centroid samples to stay conservative.
- `build_terrain_solid` emits top triangles, a vertical skirt along the border,
  and a flat bottom at `-thickness`, all consistently wound. The skirt is one
  quad per border segment (every border node carries its own bottom vertex), so
  it follows the border profile exactly; a corner fan would bridge concave
  stretches and leave a zero-thickness wall hanging above the low ground.

### 6. Footprint cleanup and extrusion

- `make_valid` / `buffer(0)` repairs self-intersections.
- Rings are oriented counter-clockwise (exterior) with holes preserved.
- Slivers below `min_building_area_m2` and holes below 1 m² are dropped.
- Optional simplification uses `preserve_topology=True`.
- `trimesh.creation.extrude_polygon` triangulates the polygon (mapbox-earcut)
  and creates top/bottom caps and side walls.
- Degenerate faces are removed, winding/normals are repaired, and each shell is
  checked for watertightness, consistent winding, finite vertices, and positive
  volume before it is accepted.
- If `base_m <= 0` and `embed_depth_m > 0`, the shell is extended below its
  ground height by the embed depth. This avoids a coplanar
  building-bottom/ground interface.
- With terrain, the shell is translated to `min_over(footprint)` first, so the
  embedded bottom sits inside the hill and the union cuts the building at the
  real ground line. Footprints spanning more than 3 m of relief raise
  `STEEP_FOOTPRINT`.

Batch work is split in two phases: `prepare_buildings` projects, cleans, and
resolves heights (and returns the union of all footprint bounds so the terrain
can cover them), then `extrude_prepared` places and extrudes each shell.
`build_meshes` remains as a flat-ground single-call wrapper.

Individual heights and source attributes are preserved in the building records
until extrusion. Overlaps are still counted with a Shapely `STRtree` and
reported as `OVERLAP_DETECTED`, but all accepted shells now enter the city union.

### 7. Ground, fusion, and STL

The base is always a closed solid covering the union of the selected bbox and
all building footprints plus `ground_margin_m`:

- terrain enabled: the `build_terrain_solid` block, top = DEM, bottom =
  `-ground_slab_thickness_m`;
- terrain disabled: a flat box from `-ground_slab_thickness_m` to `0`.

There is no open plane mode.

`app/services/city.py` performs the fusion:

1. Boolean-union all closed building shells plus the slab with Manifold.
2. Validate faces, watertightness, winding, finite vertices, volume, and
   non-manifold edges.
3. Write the result to binary STL, read it back, and check watertightness and
   non-manifold edges again.
4. If the round-trip fails, merge vertices in memory, collect every edge with
   more than two incident faces, cluster nearby bad edges, add one solid box
   with 0.4 m padding around each cluster, and repeat the union. Up to
   `MAX_WELD_PASSES` passes are allowed.
5. If the result still fails, raise `CITY_UNION_FAILED`; never fall back to
   concatenation.

Touching buildings have no shared interior, so a mathematically manifold single
solid requires a small amount of added material at the contact. The weld columns
are that material. `city.weld_repairs` records how many were needed.

Binary STL is written with an 80-byte header, uint32 triangle count, and 50 bytes
per triangle. `write_outputs` validates the loaded file again and records
`triangle_count`, `watertight`, `winding_consistent`, `volume_m3`,
`non_manifold_edges`, `base_thickness_m`, `terrain_triangle_count`,
`union_engine`, and `weld_repairs` under metadata `city`.

### 8. Output bundle

`write_outputs` produces `city.stl`, `patches.json`, `metadata.json`,
`buildings.geojson`, `README.txt`, and `result.zip`. There are no separate
building or ground STLs in the job files or ZIP. It also writes an internal
`preview.stl` assembled from the individual building shells and the clean base
mesh (`FusedCity.base_mesh`: the terrain block when enabled); this display-only
mesh excludes boolean welds and is served only by `/api/jobs/{id}/preview.stl`
as the fallback for jobs without a usable `patches.json`.
Writes use a temp file + replace so a crash never leaves a half-written
JSON/ZIP visible. The metadata schema version is `2.0`; it adds a `terrain`
block (`enabled`, source, zoom, grid, datum, relief, tile counts, nodata fill),
a `patches` block (`counts`, `ground_tolerance_m`, `ground_normal_dot_min`,
`range_count`, `file`),
`ground.surface`/`mode`, and the per-building
`ground_m`/`roof_m`/`footprint_relief_m` in `buildings.geojson`.

`patches.json` classifies every triangle of the written STL as `ground`
(upward, centroid and all three vertices within
`CITYGEN_PATCH_GROUND_TOLERANCE_M` of the terrain surface, and the face normal
agreeing with the terrain normal within `CITYGEN_PATCH_GROUND_NORMAL_DOT_MIN`),
`wall` (building walls, roofs, and the vertical terrain skirt), or `bottom`
(every vertex on the mesh's lowest plane, i.e. the base underside). Ranges are
contiguous and in STL order; the classifier runs on the reloaded `city.stl`, so
the order is exactly the file's. The vertex test keeps a near-surface roof
triangle on a steep slope out of `ground`, and the normal test keeps a
near-vertical wall sliver that touches the ground plane out as well. The
geometric bottom rule keeps float32 noise on short vertical wall triangles from
creating a second bottom patch. `TerrainGrid.surface_normal` mirrors
`surface_z`'s cell split to return the exact TIN normal.

## Frontend

- `app/static/js/map.js` wraps Leaflet: OSM tiles, two-click rectangle
  selection, live preview, area calculation with haversine distances, and
  selection readout.
- `app/static/js/api.js` is a small fetch wrapper with structured API errors.
- `app/static/js/app.js` owns UI state: config defaults, form → request payload,
  polling, progress, warnings, result cards, and cancel.
- `app/static/js/viewer.js` lazily imports Three.js and `STLLoader` only when the
  preview is opened. STL is Z-up; the viewer rotates it to Y-up, centers it,
  fits the projected model (not its bounding sphere), and places it on a
  rounded display plinth. When the job has `patches.json`, the viewer loads the
  fused `city.stl`, splits its non-indexed geometry by the patch ranges (one
  mesh per `ground`/`wall`/`bottom`) and gives each kind its own material, so
  the CFD boundary groups are visible; the footer legend lists the counts.
  Jobs without usable patches fall back to `preview.stl` and one warm-white
  material. Soft
  directional lighting, a PMREM studio environment, and a radial contact shadow
  give the model a physical, printed look. The renderer enables a logarithmic
  depth buffer so large ground surfaces do not z-fight at grazing angles. The
  viewer offers left-drag orbit, middle-drag pan, wheel zoom, reset, auto-rotate,
  and wireframe controls. Downloaded files remain Z-up.
- The layout is responsive: map + 390 px sidebar on desktop; stacked map and
  sidebar below 980 px; single-column fields below 620 px. The viewer footer
  wraps its legend and controls below 620 px.

The frontend deliberately has no build chain. This keeps deployment to
`pip install` + `uvicorn`, at the cost of relying on CDN availability for
Leaflet/Three.js and OSM tiles.

## Error model

Domain errors carry a stable `code`, a user-facing Chinese `message`, and an
optional `detail`:

| Code | Meaning |
| --- | --- |
| `AREA_TOO_LARGE` | Selection exceeds `max_area_km2`. |
| `NO_BUILDINGS` | Overpass returned no usable building footprints. |
| `TOO_MANY_BUILDINGS` | Feature count exceeds `max_buildings`. |
| `OVERPASS_FAILED` | All endpoints/retries failed. |
| `OSM_PARSE_FAILED` | OSM JSON could not be parsed. |
| `GEOMETRY_ERROR` | Geometry construction failed. |
| `TERRAIN_FETCH_FAILED` | DEM tiles could not be downloaded after retries. |
| `TERRAIN_DATA_MISSING` | The DEM has no usable elevation for the selection. |
| `CITY_UNION_FAILED` | Buildings and ground could not be fused into a closed city. |
| `JOB_CANCELLED` | Cancellation observed by the worker. |
| `SERVER_RESTARTED` | Recovered job from a previous process. |
| `INTERNAL_ERROR` | Unexpected worker exception (logged). |

The API returns `404` for unknown jobs/files, `409` for not-ready downloads or
a job that is still stopping, and `422` for schema validation failures.

## Concurrency and storage

- Two worker threads by default (`CITYGEN_MAX_WORKERS`), so a second large job
  queues instead of competing for CPU/memory.
- `job.json` is the source of truth; the API never depends on in-memory state
  for status.
- Cancellation is cooperative: the pipeline checks a `threading.Event` between
  stages and every building.
- Overpass requests are throttled per process; cached responses avoid repeated
  load on public endpoints. Large selections are fetched as ≤4 km² cells, so one
  timed-out request only costs one cell, and cells are reused across runs.
- Job/output paths are derived from a UUID and file names are allow-listed;
  path traversal is rejected.

## Performance notes

The dominant cold-start cost is the public Overpass request (often 30–60 s for
a sub-km² area). With raw OSM cached, the remaining work is parsing, polygon
cleanup, extrusion, and STL writing. For a 671-building / 0.98 km² area
(≈1 km × 1 km) on the development machine:

- cold: ~55 s (Overpass ~49 s);
- cached: ~8.1 s.

`metadata.json → timings_s` records `fetch`, `parse`, `prepare`, `terrain`,
`build`, `ground`, `fuse`, `export`, and `total` for every run (a snapshot taken
just before `metadata.json` is written; the zip comes after).

For the cached 4×4 km Chongqing sample (1 696 buildings, 84 654 triangles,
39 460 terrain triangles) `total` is ≈14–15 s: `build` ≈8–10 s dominates, `fuse`
≈2.2–2.5 s, `export` ≈1.2–1.8 s. Profiling found the boolean union itself is
only ≈0.3 s — `trimesh` already runs a balanced pairwise cascade, `Manifold`'s
`batch_boolean` is only marginally faster, and grouping shells across threads
was slower (the Manifold bindings hold the GIL), so the post-union work was the
lever: the collapsed-vertex nudge, the non-manifold edge census, and repeated
validation. Those now cost about half of what they did (fuse 4.41 → ≈2.4 s on
the earlier 81 864-triangle geometry, with identical triangles, volume, zero
welds, watertight). Sustained CPU load throttles the development machine by
roughly 4×, so compare before/after runs interleaved rather than against an
idle-machine baseline.

Per-building geometry itself is fast (about 0.6 s for 400 buildings in a
profile); the original cache-hit bottleneck was rewriting `job.json` for every
building, which is now throttled.

Terrain adds a one-off DEM download (2 tiles, ~1 MB, ≈1–3 s cold, cached after
that) plus grid sampling and the boolean union. For 748 buildings over a
1.1 km × 1.1 km grid at 30 m (≈3.0 k terrain triangles, 26.7 k in total) the
terrain solid and union take ≈0.2 s, and `city.stl` is ≈1.3 MB.

## Deliberate non-goals

No roads, vegetation, water, pitched roofs, `building:part`, volumetric mesh,
boundary conditions, solver case generation, or multi-user authentication. The
terrain is a DEM grid, not a survey: no cliff, building, or vegetation detail,
and no stream carving. Boolean fusion is now part of the pipeline, but it is
deliberately limited to producing one closed surface; it does not reconstruct
architectural details or merge touching buildings into shared walls.
