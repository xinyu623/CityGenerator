# Changelog

All notable changes to CityGenerator are recorded here.

## [Unreleased]

- CI: GitHub Actions runs `ruff check app tests` and the offline `pytest` suite
  on every push and pull request.
- Golden geometry test: `tests/test_golden.py` builds a fixed 300 m
  25-building block on a synthetic terrain ramp and compares triangle count,
  volume, surface area, bounds, weld repairs, watertightness, non-manifold
  edges, and the maximum terrain border step against
  `tests/golden/city_300m.json` (tolerances; refresh with `UPDATE_GOLDEN=1`).
- Untagged buildings now prefer heights from similarly sized OSM buildings: each
  known height is paired with its footprint area, and a draw for a target
  footprint uses observations within 0.25–4× its area, falling back to the whole
  pool when nothing matches. `metadata.json → buildings.height_sampling` gains
  `area_weighted` and `area_ratio`. On the 4×4 km sample the sampled heights
  correlate with footprint area (log-log Pearson 0.36; median 3.1 m under
  2 000 m² vs 33.5 m above), where before a shed could draw a tower height.
- `patches.json` now ships next to `city.stl`: contiguous triangle ranges
  labelled `ground` (upward and within `CITYGEN_PATCH_GROUND_TOLERANCE_M` =
  0.05 m of the terrain surface), `wall` (building walls, roofs, and the
  vertical terrain skirt), or `bottom` (the base underside). Ranges follow the
  STL triangle order, so a solver can assign CFD boundary patches in one pass
  without splitting the closed surface. Metadata gains a `patches` block with
  the counts, tolerance, and range count; the file is listed in
  `metadata.json → files`, `result.zip`, and `README.txt`. On the 4×4 km sample:
  53 041 ground / 28 275 wall / 548 bottom triangles (993 ranges); on the
  0.74 km² sample: 10 303 / 22 317 / 146 (471 ranges).
- A large selection no longer fails when one Overpass chunk keeps timing out:
  after the endpoint retries are exhausted the cell is retried as four sub-cells,
  recursively at most `CITYGEN_OVERPASS_SPLIT_MAX_DEPTH` levels (2) and never
  below `CITYGEN_OVERPASS_SPLIT_MIN_AREA_KM2` (0.25 km²). Sub-cells are cached
  under their own bbox and the merged payload under the parent, so a later run
  is served from disk. `metadata.json → source.overpass_splits` records each
  split (bbox, depth, child count, error) and `source.overpass_cell_times` the
  elapsed time of every cell, including cache hits. A cell that cannot be
  subdivided still fails the job.
- Every run records stage wall times in `stats["timings_s"]` and
  `metadata.json → timings_s` (`validate`, `fetch`, `parse`, `project`,
  `prepare`, `terrain`, `build`, `ground`, `fuse`, `export`, `total`), so a
  performance change is measurable from the output alone. The timings are a
  snapshot taken just before `metadata.json` is written, so the metadata write
  and the result zip are not included.
- Fusion is about 2× faster. The float32-collapse nudge now looks up one
  packed integer key per neighbour cell instead of building 27 coordinate
  tuples per vertex; the non-manifold edge census packs each sorted edge into
  one integer key instead of sorting a structured array; and the fused mesh is
  validated once instead of twice (in `fuse_city` and again for the written
  STL). On the cached 4×4 km sample (1 696 buildings) `fuse` drops 4.41 → 2.18 s
  and the whole run 17.5 → 13.9 s, with identical triangles (81 864), volume
  (1.32476e9 m³), zero weld repairs, watertight and zero non-manifold edges.
  The boolean union itself was already a balanced pairwise cascade (≈0.3 s);
  `Manifold.batch_boolean` saved only ~0.1 s and grouping across threads was
  slower, so the post-union validation was the real lever.
- Headless CLI: `citygen --bbox s,w,n,e --out DIR` (or `python -m app.cli`)
  runs the whole pipeline without uvicorn and returns a non-zero exit code with
  the error code on failure.
- Touching footprints are grown by `CITYGEN_CONTACT_OVERLAP_M` (0.1 m total)
  before extrusion, so neighbouring buildings share real volume instead of
  meeting at a zero-thickness edge.
- Untagged buildings now sample their height from the selection's own OSM
  heights (`CITYGEN_HEIGHT_SAMPLING_*`, ±10 % jitter, deterministic seed) instead
  of all receiving the same 25 m default. Selections with fewer than
  `height_sampling_min_samples` known heights keep the old fallback.
  `metadata.json → buildings.height_sampling` records the pool size, jitter,
  seed, and how many buildings were sampled.
- Binary-STL float32 collapse fixed: vertices closer than 0.5 mm are nudged
  apart before export, so the round-trip no longer invents non-manifold edges.
  The 4×4 km sample needs **0 weld repairs** (was 50) and 81 864 triangles
  (was 112 380).

## [2.0.0] - 2026-11-02

Terrain relief. The city now sits on a real DEM surface instead of a flat slab,
still fused into one watertight `city.stl`.

### Terrain

- New `terrain` block in the request options:
  `{"enabled": true, "resolution_m": 30.0}`. Terrain is **on by default**.
- Large selections are downloaded in Overpass chunks: anything above
  `CITYGEN_OVERPASS_CHUNK_AREA_KM2` (`4 km²`, so at most 2 km per side) is split
  into a grid of cells, fetched and cached individually, then merged with
  duplicate OSM elements removed. The default selection cap rises from `4 km²`
  to `17 km²` (an exact 4 × 4 km box is 16.005 km²); `AREA_TOO_LARGE` still
  rejects anything above `CITYGEN_MAX_AREA_KM2`. A failed cell fails the job
  instead of silently dropping buildings.
- Elevations come from the AWS Terrain Tiles (Terrarium) open dataset, decoded
  from 8-bit RGB PNG and resampled onto a local grid (`5–50 m` spacing, capped
  by `CITYGEN_MAX_TERRAIN_CELLS`).
- The sampled field is median-smoothed over ~150 m (`metadata.json →
  terrain.smoothing_m` / `terrain.filter`). The source is a surface model with
  building and canopy bias, and single bad cells otherwise grow into 100 m-wide
  domes that do not exist on the ground. The default spacing is 30 m, the native
  resolution of the data, which also cuts terrain triangles by roughly 8×
  compared with the first 10 m implementation.
- Tiles are cached under `data/cache/dem/` and pruned with the same TTL as the
  OSM cache.
- The terrain block is a closed solid: DEM top, vertical skirt, flat bottom at
  `-ground_slab_thickness_m`, so `city.stl` stays watertight with zero
  non-manifold edges. The skirt is one vertical quad per border segment, so it
  follows the border profile exactly. A single corner fan used to bridge dips in
  the border and left a zero-thickness wall hanging above the low ground (up to
  the selection relief), visible as a raised rim around low terrain and a
  zero-thickness obstacle for the solver.
- `z = 0` is the lowest terrain cell in the selection.
  `metadata.json → terrain.datum_offset_m` restores absolute elevations.
- Each building sits on `min_over(footprint)` and is embedded `0.5 m` below it;
  slopes cut the building at the natural ground line. Footprints spanning more
  than `3 m` of relief raise `STEEP_FOOTPRINT`.
- New warnings: `TERRAIN_RESOLUTION_REDUCED`, `TERRAIN_RESOLUTION_LIMITED`,
  `TERRAIN_ZOOM_REDUCED`, `TERRAIN_NODATA_FILLED`, `TERRAIN_FLAT_RELIEF`,
  `STEEP_FOOTPRINT`.
- New errors: `TERRAIN_FETCH_FAILED` (tiles unavailable after retries) and
  `TERRAIN_DATA_MISSING` (no usable elevation). A DEM failure never silently
  becomes a flat site; disable terrain to generate anyway.
- Setting `terrain.enabled: false` reproduces the flat v1 output.

### Output contract

- Metadata schema version is now `2.0`. New `terrain` block, `ground.mode`
  (`terrain` / `slab`) and `ground.surface`, `city.terrain_triangle_count`, and
  `local_frame.z_reference`.
- `buildings.geojson` features gain `ground_m`, `roof_m`, and
  `footprint_relief_m`.
- `README.txt` gains a terrain section with source, grid, relief, and datum.
- The preview mesh uses the same base as the export (terrain block when
  enabled).
- No new output files: still exactly one `city.stl`.

### API and UI

- `GET /api/config` returns `defaults.terrain` and `terrain_attribution`.
- New `terrain` progress stage; `building` now appears before and after it.
- The `fetching` stage reports per-chunk progress (`已下载 n/N 个 OSM 分块`), so a
  large selection no longer sits at 4 % while several requests run.
- `metadata.json → source.overpass_chunks` records how many Overpass requests a
  job needed.
- The UI exposes a terrain toggle and resolution field (default `30 m`), shows
  the selection's terrain relief in the result stats, and hints that terrain can
  be disabled when a DEM request fails.

### Dependencies and internals

- Added `pillow>=10` for PNG decoding.
- Building work is split into `prepare_buildings` + `extrude_prepared`;
  `build_meshes` remains as a flat-ground wrapper.
- `fuse_city(..., base_thickness_m=...)` keeps a `base_mesh` for previews.
- New tests: terrain sampling/solid/placement (`tests/test_terrain.py`), including
  that a single bad DEM cell is suppressed by the smoothing, DEM
  decoding/caching/zoom/errors (`tests/test_dem.py`), Overpass chunking, merge
  de-duplication, per-chunk caching, chunk progress and chunk failure policy
  (`tests/test_overpass.py`), optional live DEM smoke test
  (`tests/test_live_dem.py`, marked `network`).

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
