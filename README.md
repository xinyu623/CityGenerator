# CityGenerator

Generate a simulation-ready, **closed city solid** from OpenStreetMap (OSM)
building footprints for outdoor wind-environment CFD (OpenFOAM, STAR-CCM+,
Fluent, and similar solvers).

The app is a local FastAPI service with a browser UI:

1. Pan/zoom an OpenStreetMap basemap and drag a rectangle.
2. Set the default building height (25 m by default), level height, and geometry
   cleanup options.
3. The backend fetches OSM footprints from Overpass, projects them into a local
   metric frame, extrudes closed building shells, builds a solid ground slab, and
   fuses everything into **one watertight `city.stl`**.
4. Download a ZIP or inspect the result in the built-in Three.js viewer.

All geometry is generated locally. OSM data is the only external input and is
cached on disk.

[Chinese README](README.zh-CN.md)

![CityGenerator UI](docs/images/frontend.png)

The built-in viewer presents the fused city as a warm-white architectural model
on a display plinth, with soft studio shadows and orbit controls. The viewer
splits the single STL into building and ground materials by triangle height for
display only; the exported file remains one Z-up mesh.

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

Open <http://127.0.0.1:8000/>. Use **Select area**, click two opposite corners,
adjust settings, and press **Generate STL**.

The UI is in Chinese; code, comments, metadata keys, and this README are in
English. A Chinese translation is available in [README.zh-CN.md](README.zh-CN.md).

## Output

A completed job contains:

| File | Contents |
| --- | --- |
| `city.stl` | Binary STL. One fused, closed, outward-oriented city solid: buildings plus a 1 m ground slab. |
| `metadata.json` | Schema version, local origin, CRS, options, statistics, warnings, source provenance, and city validation results. |
| `buildings.geojson` | WGS84 footprints with height, base, source, and selected tags. |
| `README.txt` | Short human-readable notes and CFD import guidance. |
| `result.zip` | ZIP containing the four files above (it does not contain itself). |

There are **no separate `buildings.stl` or `ground.stl` files**. The city is
intended to be imported as one closed obstacle.

### How the city is fused

1. Each valid building is extruded as a closed shell. Its bottom is embedded
   `0.5 m` below `z = 0` by default.
2. A 1 m thick solid slab is built under the union of the selected area and all
   building footprints, expanded by `ground_margin_m`.
3. The building shells and slab are boolean-unioned with
   [Manifold](https://github.com/elalish/manifold). Internal faces disappear.
4. When two buildings touch only at an edge or corner, their volumes have no
   common interior, so the union is geometrically non-manifold. The pipeline
   detects those exact edges after an STL round-trip, clusters nearby bad edges,
   and adds a small solid weld box (0.4 m padding) around each cluster before
   re-running the union. The number of boxes is recorded as
   `city.weld_repairs`.
5. The written STL is loaded again and must pass watertightness, consistent
   winding, finite vertices, positive volume, and zero non-manifold edges. A
   failure is reported as `CITY_UNION_FAILED`; the pipeline never silently falls
   back to a concatenated triangle soup.

Most city models need zero or a small number of welds; dense areas with many
touching footprints can need more. They are local contact repairs, not a change
to the overall building layout.

**Why not just add a plane under everything?** The slab already closes the
underside; the failure is above the base, where two building volumes meet along
a vertical line without sharing interior volume. No bottom plane can turn that
line contact into a manifold surface. A single closed solid requires a tiny
material bridge at the contact (or a gap that keeps the buildings separate).
The weld boxes are that bridge.

### Coordinate convention

- Units: metres.
- `+X` = east, `+Y` = north, `+Z` = up (the STL is Z-up).
- Origin: south-west corner of the selected WGS84 bounding box, projected to the
  appropriate UTM zone (`EPSG:326xx` north / `EPSG:327xx` south).
- `metadata.json → local_frame.origin` records the origin's easting, northing,
  longitude, and latitude so the model can be georeferenced again.

Useful `metadata.json → city` fields:

```json
{
  "triangle_count": 7136,
  "watertight": true,
  "winding_consistent": true,
  "volume_m3": 12294326.099,
  "non_manifold_edges": 0,
  "base_thickness_m": 1.0,
  "union_engine": "manifold",
  "weld_repairs": 8
}
```

## CFD import notes

1. Import `city.stl` as one closed obstacle patch or solid. You no longer need
   to combine two STL files.
2. Confirm the solver reads the file as metres and that `+Z` is up. If your
   solver uses Y-up, rotate `-90°` about X.
3. Create the fluid domain around the city: inlet, outlet, sides, and top.
   Recommended starting distances are 5H upstream, 10–15H downstream, 5H to the
   sides, and 5H above the tallest building, where H is the tallest height.
4. The ground is part of the solid, so there is no open ground patch to close.
   The underside of the slab is a valid closed boundary.
5. Check `MISSING_HEIGHT` and `HEIGHT_CLAMPED` warnings. The default 25 m height
   is a placeholder, not a surveyed value.
6. Review `city.weld_repairs` if you need to know whether touching footprints
   required local contact repairs.
7. This tool stops at surface STL. It does not create a volume mesh, boundary
   layers, turbulence settings, or solver dictionaries.

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
    "ground_margin_m": 0,
    "simplify_m": 0.05,
    "min_building_area_m2": 4
  },
  "refresh": false
}
```

`bbox` must have `north > south` and `east > west`. `refresh` bypasses the
Overpass cache for that bounding box.

Progress stages: `queued`, `starting`, `validating`, `fetching`, `parsing`,
`projecting`, `building`, `ground`, `fusing`, `exporting`, `completed` (plus
`failed`, `cancelling`, `cancelled`). The UI polls roughly once per second.

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
CITYGEN_GROUND_SLAB_THICKNESS_M=1.0
```

Useful defaults:

- Maximum selection area: `4 km²`.
- Maximum accepted buildings: `20,000`.
- Default height: `25 m`; level height: `3 m`.
- Bottom embed depth: `0.5 m`; polygon simplification: `0.05 m`.
- Ground slab thickness: `1 m`.
- Job retention / Overpass cache retention: `7 days`.
- Worker threads: `2`.

Cache and jobs live under `data/cache/osm/` and `data/jobs/<job_id>/`. Both are
ignored by git.

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

The suite includes a round-trip regression test for edge-touching buildings: the
fused city must remain watertight and have zero non-manifold edges after being
written to binary STL and loaded again.

## Limits (v1)

No DEM/terrain, roads, vegetation, water, pitched roofs, `building:part`, or
volumetric meshing. Every building is a vertical prism; touching footprints are
fused with local weld repairs rather than merged into shared architectural
geometry. Multi-user auth, queue persistence across restarts, and distributed
workers are out of scope.

## Data licence and attribution

OSM data is © OpenStreetMap contributors, licensed under
[ODbL 1.0](https://www.openstreetmap.org/copyright). Attribution is embedded in
`metadata.json`, `README.txt`, the UI, and this document. Generated geometry
derived from OSM remains subject to ODbL attribution/share-alike requirements.

Frontend libraries: [Leaflet](https://leafletjs.com/) (BSD-2-Clause) and
[Three.js](https://threejs.org/) (MIT), loaded from jsDelivr. Basemap tiles are
served by the OpenStreetMap tile servers.
