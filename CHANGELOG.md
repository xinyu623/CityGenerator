# Changelog

All notable changes to CityGenerator are recorded here.

## [1.0.0] - 2026-10-30

First frozen release.

### Output contract

- Produces exactly one CFD STL: `city.stl`.
- `city.stl` is a single closed, outward-oriented solid made from all accepted
  building shells plus a solid ground base.
- Ground base thickness is `1.0 m` by default (`z=-1.0` to `z=0`).
- Building bottoms are embedded `0.5 m` below `z=0` before the union.
- Buildings and base are fused with Manifold; touching edge/corner contacts are
  repaired with local weld boxes until the binary STL round-trip is watertight
  with zero non-manifold edges.
- If the city cannot be closed, the job fails with `CITY_UNION_FAILED`; there is
  no concatenation fallback.
- No separate `buildings.stl` or `ground.stl` is written.
- Metadata schema version: `1.0`.

### API and files

- Job files: `city.stl`, `metadata.json`, `buildings.geojson`, `README.txt`;
  `result.zip` contains those four files.
- `GET /api/jobs/{job_id}/preview.stl` serves an internal display-only mesh.
  It is not listed in job files and is not included in `result.zip`.
- `GET /api/config` no longer exposes `include_ground`, `ground_mode`, or
  `export_mode`.
- Default building height is `25 m`.

### Preview

- The viewer uses one material for the fused city.
- It loads the cleaned display-only preview mesh (individual building shells +
  clean base, without boolean contact patches).
- Thin preview triangles are filtered.
- A logarithmic depth buffer prevents ground z-fighting at grazing angles.
- Left drag orbits, middle drag pans, wheel zooms.
- The preview is presentation-only; `city.stl` remains the CFD output.

### Documentation

- English `README.md` and Chinese `README.zh-CN.md`.
- Architecture and OSM-to-STL notes describe the fused city, validation, and
  preview pipeline.

### Known limits

- Flat ground only; no DEM/terrain.
- No roads, vegetation, pitched roofs, `building:part`, or volume meshing.
- Touching footprints are fused with local weld patches rather than
  reconstructed as shared architectural walls.
