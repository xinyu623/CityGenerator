# CityGenerator implementation plan — OSM to CFD STL

Status: approved 2026-10-29.

## Goal

Local web application: draw a rectangle on an OSM map, fetch building footprints
from Overpass, project to a local metric frame, extrude closed building shells,
generate a ground patch, and download a ZIP with binary STL files plus
`metadata.json` for outdoor wind CFD (OpenFOAM / STAR-CCM+).

## Architecture

- Backend: Python 3.11+, FastAPI, uvicorn, httpx, Pydantic v2.
- Geometry: Shapely 2.x, pyproj, numpy, trimesh, mapbox-earcut.
- OSM parsing: `osm2geojson` fed by Overpass JSON.
- Frontend: static ES modules, Leaflet, Three.js; served by FastAPI.
- Jobs: `ThreadPoolExecutor`, disk-backed state, cancellation event.
- Output: `buildings.stl`, `ground.stl`, `city.stl`, `metadata.json`,
  `buildings.geojson`, `README.txt`.

## Tasks

1. Scaffold: package, config, health, static placeholder, test harness.
2. OSM layer: Overpass client, retry/rate limit, cache, `osm2geojson` parser.
3. Geometry core: UTM projection, height parsing, polygon cleanup, extrusion.
4. Export: ground, STL, metadata, ZIP, re-read validation.
5. Jobs/API: thread pool, disk state, progress, cancel, download, errors.
6. Frontend flow: map rectangle, options, submit, poll, result.
7. 3D preview: Three.js STLLoader with camera fit.
8. Tests/docs: offline fixtures, API tests, end-to-end manual verification.

## Non-goals for v1

DEM terrain, roads, vegetation, pitched roofs, `building:part`, boolean union
into a single watertight solid, volumetric mesh generation, multi-user auth.

## Acceptance

- 1 km x 1 km generation under 3 minutes including Overpass; cache hit under 10 s.
- Every exported building shell is watertight with outward normals.
- Metadata round-trips local coordinates to WGS84.
- Missing heights/invalid geometry/overlaps produce warnings.
- Offline unit and API tests pass; optional live smoke test is marked.
