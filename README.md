# CityGenerator

Generate simulation-ready STL building models from OpenStreetMap (OSM) data for
outdoor wind-environment CFD (OpenFOAM, STAR-CCM+, Fluent, etc.).

The app is a local FastAPI service with a browser UI:

1. Pan/zoom an OpenStreetMap basemap and drag a rectangle.
2. Choose default height, level height, ground mode, and export options.
3. The backend fetches OSM footprints from Overpass, projects them to a local
   metric frame, extrudes closed building shells, builds a ground patch, and
   writes binary STL files plus metadata.
4. Download a ZIP or inspect the result in the built-in Three.js viewer.

All geometry is generated locally. OSM data is the only external input (and is
cached on disk).

![CityGenerator UI](docs/images/frontend.png)

The built-in 3D viewer presents the two STL patches as a warm-white architectural
model on a display plinth, with soft studio shadows and orbit controls. It is a
presentation layer only; the exported geometry remains the unchanged Z-up STL.

![CityGenerator 3D preview](docs/images/preview.png)

## Quick start

Requirements: Python 3.11+, internet access for the first Overpass request and
for the basemap/Three.js CDN assets.

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

```bash
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000/>. Use the **框选区域** button, click two opposite
corners, adjust parameters, and press **生成 STL**.

The UI is in Chinese; code, comments, metadata keys, and docs are in English.

## Output

A completed job contains:

| File | Contents |
| --- | --- |
| `buildings.stl` | Binary STL; one closed, outward-oriented shell per building. |
| `ground.stl` | Ground patch at `z = 0` (2 triangles) or a 1 m thick slab (12 triangles). |
| `city.stl` | `buildings.stl` + `ground.stl` concatenated for quick preview/import. |
| `metadata.json` | Schema version, local origin, CRS, options, statistics, warnings, source provenance. |
| `buildings.geojson` | WGS84 footprints with height, base, source, and selected tags. |
| `README.txt` | Human-readable notes and CFD import guidance. |
| `result.zip` | ZIP containing all of the above. |

Coordinate convention:

- Units: metres.
- `+X` = east, `+Y` = north, `+Z` = up (the STL is Z-up).
- Origin: south-west corner of the selected WGS84 bounding box, projected to the
  appropriate UTM zone (`EPSG:326xx` north / `EPSG:327xx` south).
- `metadata.json → local_frame.origin` records the origin's easting, northing,
  longitude, and latitude so the model can be georeferenced again.

`buildings.stl` is a multi-shell obstacle file, not a single watertight solid.
Each building shell is individually checked for watertightness, consistent
winding, finite vertices, and positive volume. Adjacent or overlapping
buildings can still create non-manifold contacts when the STL is re-read as one
mesh; the UI reports `NON_MANIFOLD_CONTACTS` when that happens. `city.stl` is
intentionally a concatenation and is **not** watertight.

## CFD import notes

1. Import `buildings.stl` and `ground.stl` as separate patches. Keeping them
   separate is usually easier than splitting `city.stl` again.
2. Building bottoms are embedded 0.5 m below `z = 0` by default so side walls do
   not share a coplanar face with the ground.
3. Build the fluid domain around the model and mesh it in your solver. This tool
   does not create a volume mesh, a case directory, or boundary conditions.
4. If your solver needs one closed surface for the fluid domain, run a surface
   wrap / boolean union in your preprocessor first. The raw OSM shells are
   intentionally not boolean-unioned, because that would destroy per-building
   heights and attributes.
5. Check units and orientation after import. A wind inlet can initially be
   aligned with `+X` (east) or `+Y` (north); rotate the model or the domain as
   needed.
6. Missing `height` tags use `building:levels × level height`, then the default
   height (25 m). Review `MISSING_HEIGHT` warnings before production runs.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness and version. |
| `GET` | `/api/config` | Limits, defaults, attribution, tile URL. |
| `POST` | `/api/jobs` | Create a generation job. Returns `202` and `job_id`. |
| `GET` | `/api/jobs/{job_id}` | Status, progress, stage, stats, warnings, files. |
| `GET` | `/api/jobs/{job_id}/download` | Download `result.zip` (completed jobs only). |
| `GET` | `/api/jobs/{job_id}/files/{name}` | Download a single output file. |
| `DELETE` | `/api/jobs/{job_id}` | Cancel a running job or delete a terminal job. |

Example request:

```json
{
  "bbox": { "south": 28.19, "west": 112.97, "north": 28.196, "east": 112.978 },
  "options": {
    "default_height_m": 25,
    "level_height_m": 3,
    "embed_depth_m": 0.5,
    "include_ground": true,
    "ground_mode": "plane",
    "ground_margin_m": 0,
    "simplify_m": 0.05,
    "min_building_area_m2": 4,
    "export_mode": "both"
  },
  "refresh": false
}
```

`bbox` must have `north > south` and `east > west`. `refresh` bypasses the
Overpass cache for that bounding box.

Progress stages: `queued`, `starting`, `validating`, `fetching`, `parsing`,
`projecting`, `building`, `ground`, `exporting`, `completed` (plus `failed`,
`cancelling`, `cancelled`). The UI polls roughly once per second.

## Configuration

Settings use the `CITYGEN_` prefix and can be placed in `.env`:

```dotenv
CITYGEN_DATA_DIR=./data
CITYGEN_MAX_AREA_KM2=4
CITYGEN_MAX_BUILDINGS=20000
CITYGEN_MAX_WORKERS=2
CITYGEN_OVERPASS_ENDPOINTS=https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter
CITYGEN_OVERPASS_TIMEOUT_S=180
CITYGEN_OVERPASS_MIN_INTERVAL_S=1.0
CITYGEN_JOB_TTL_DAYS=7
CITYGEN_CACHE_TTL_DAYS=7
```

Useful defaults:

- Maximum selection area: `4 km²`.
- Maximum accepted buildings: `20,000`.
- Default height: `25 m`; level height: `3 m`.
- Bottom embed depth: `0.5 m`; polygon simplification: `0.05 m`.
- Ground slab thickness: `1 m`.
- Job retention / Overpass cache retention: `7 days`.
- Worker threads: `2`.

Cache and jobs live under `data/cache/osm/` and `data/jobs/<job_id>/`.
Both are ignored by git.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m ruff check app tests
```

Tests are offline by default: Overpass is mocked and the OSM fixture is
synthetic. An optional live Overpass smoke test is marked `network` and skipped
unless explicitly enabled:

```powershell
$env:CITYGEN_RUN_NETWORK = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_live_overpass.py -q -m network
```

Measured on the development machine (network and machine dependent):

- 0.98 km² (≈1 km × 1 km), 671 buildings: ~55 s cold (mostly Overpass),
  ~8.1 s with cached OSM.
- 0.52 km², 404 buildings: ~58 s cold, ~6.5 s cached.

## Limits (v1)

No DEM/terrain, roads, vegetation, water, pitched roofs, `building:part`,
boolean union into one solid, or volumetric meshing. Overlapping OSM footprints
are preserved and reported rather than silently merged. Multi-user auth,
queue persistence across restarts, and distributed workers are out of scope.

## Data licence and attribution

OSM data is © OpenStreetMap contributors, licensed under
[ODbL 1.0](https://www.openstreetmap.org/copyright). Attribution is embedded in
`metadata.json`, `README.txt`, the UI, and this document. Generated geometry
derived from OSM remains subject to ODbL attribution/share-alike requirements.

Frontend libraries: [Leaflet](https://leafletjs.com/) (BSD-2-Clause) and
[Three.js](https://threejs.org/) (MIT), loaded from jsDelivr. Basemap tiles are
served by the OpenStreetMap tile servers.
