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
| `app/services/heights.py` | Parse `height`, `building:levels`, `min_height`, clamp. |
| `app/services/buildings.py` | Clean/project footprints, extrude shells, overlap detection. |
| `app/services/ground.py` | Solid ground slab. |
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
| `fetching` | 4–24 | Overpass cache lookup or HTTP request. |
| `parsing` | 24–34 | OSM JSON → Shapely footprints. |
| `projecting` | 34 | UTM projector + local origin. |
| `building` | 40–80 | Project/clean/extrude each footprint. |
| `ground` | 82 | Solid 1 m ground slab. |
| `fusing` | 86 | Boolean-union buildings and base; weld edge contacts. |
| `exporting` | 90 | STL validation, metadata, GeoJSON, ZIP. |
| `completed` | 100 | Terminal. |

The exact percentages are advisory; the UI shows the stage label and message.

## Data flow

### 1. Overpass

`OverpassClient` builds a versioned query (`buildings-v1`) over ways and
relations with a `building` tag, then fetches member nodes via `out body; >;
out skel qt;`. It tries configured endpoints in order, retries transient
failures, and enforces a minimum interval between requests. Successful raw JSON
is cached at:

```text
data/cache/osm/<sha256(query_version|bbox)>.json
```

The cache key deliberately ignores options such as default height: the raw OSM
response is independent of modelling parameters. `refresh=true` bypasses the
cache.

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

### 5. Footprint cleanup and extrusion

- `make_valid` / `buffer(0)` repairs self-intersections.
- Rings are oriented counter-clockwise (exterior) with holes preserved.
- Slivers below `min_building_area_m2` and holes below 1 m² are dropped.
- Optional simplification uses `preserve_topology=True`.
- `trimesh.creation.extrude_polygon` triangulates the polygon (mapbox-earcut)
  and creates top/bottom caps and side walls.
- Degenerate faces are removed, winding/normals are repaired, and each shell is
  checked for watertightness, consistent winding, finite vertices, and positive
  volume before it is accepted.
- If `base_m <= 0` and `embed_depth_m > 0`, the shell is extended below `z=0` by
  the embed depth. This avoids a coplanar building-bottom/ground interface.

Individual heights and source attributes are preserved in the building records
until extrusion. Overlaps are still counted with a Shapely `STRtree` and
reported as `OVERLAP_DETECTED`, but all accepted shells now enter the city union.

### 6. Ground, fusion, and STL

The ground is always a closed slab: a box from `-ground_slab_thickness_m` to `0`,
covering the union of the selected bbox and all building footprints plus
`ground_margin_m`. There is no open plane mode.

`app/services/city.py` performs the fusion:

1. Boolean-union all closed building shells plus the slab with Manifold.
2. Validate faces, watertightness, winding, finite vertices, volume, and
   non-manifold edges.
3. Write the result to binary STL, read it back, and check watertightness and
   non-manifold edges again.
4. If the round-trip fails, merge vertices in memory, collect every edge with
   more than two incident faces, cluster nearby bad edges, add one solid box
   with 0.4 m padding around each cluster, and repeat the union. Up to eight
   passes are allowed.
5. If the result still fails, raise `CITY_UNION_FAILED`; never fall back to
   concatenation.

Touching buildings have no shared interior, so a mathematically manifold single
solid requires a small amount of added material at the contact. The weld columns
are that material. `city.weld_repairs` records how many were needed.

Binary STL is written with an 80-byte header, uint32 triangle count, and 50 bytes
per triangle. `write_outputs` validates the loaded file again and records
`triangle_count`, `watertight`, `winding_consistent`, `volume_m3`,
`non_manifold_edges`, `base_thickness_m`, `union_engine`, and `weld_repairs`
under metadata `city`.

### 7. Output bundle

`write_outputs` produces `city.stl`, `metadata.json`, `buildings.geojson`,
`README.txt`, and `result.zip`. There are no separate building or ground STLs.
Writes use a temp file + replace so a crash never leaves a half-written JSON/ZIP
visible. The metadata schema version is `1.0`.

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
  rounded display plinth. Because the STL is one fused solid with no material
  groups, the viewer splits its triangles by height at a 0.05 m threshold
  (STL local Z; viewer Y after the Y-up rotation): building triangles use warm
  white, ground triangles use sand. Soft
  directional shadows, a PMREM studio environment, and a radial contact shadow
  give the model a physical, printed look. The viewer offers orbit, zoom, reset,
  auto-rotate, and wireframe controls. Downloaded files remain Z-up.
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
  load on public endpoints.
- Job/output paths are derived from a UUID and file names are allow-listed;
  path traversal is rejected.

## Performance notes

The dominant cold-start cost is the public Overpass request (often 30–60 s for
a sub-km² area). With raw OSM cached, the remaining work is parsing, polygon
cleanup, extrusion, and STL writing. For a 671-building / 0.98 km² area
(≈1 km × 1 km) on the development machine:

- cold: ~55 s (Overpass ~49 s);
- cached: ~8.1 s.

Per-building geometry itself is fast (about 0.6 s for 400 buildings in a
profile); the original cache-hit bottleneck was rewriting `job.json` for every
building, which is now throttled.

## Deliberate non-goals

No terrain/DEM, roads, vegetation, water, pitched roofs, `building:part`,
volumetric mesh, boundary conditions, solver case generation, or multi-user
authentication. Boolean fusion is now part of the pipeline, but it is deliberately
limited to producing one closed surface; it does not reconstruct architectural
details or merge touching buildings into shared walls.
