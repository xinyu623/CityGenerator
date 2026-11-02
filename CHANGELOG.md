# Changelog

All notable changes to CityGenerator are recorded here.

## [Unreleased]

- Removed the GitHub Actions workflow. The project is developed and used on a
  single Windows machine, so hosted CI covered no second environment and only
  added maintenance; `pytest`, `ruff`, and the 300 m golden test remain as local
  checks (the golden file keeps its producer-version record).

## [2.2.0] - 2026-11-02

- Sampled heights now clear a building's own tagged base. When the draw lands
  below `base + level_height_m`, the height is raised to that floor and the
  building records the new `base_floor` rule (one storey above the base rather
  than a paper-thin slab). On the 4×4 km sample this keeps two relation parts
  that used to be dropped as `TRIANGULATION_FAILED` — a 3.1/3.2 m draw on an
  18 m `building:min_level=6` base — so the reference is now 1 698 buildings,
  87 634 triangles, volume 1.33734e9 m³, patches 54 539 ground / 32 547 wall /
  548 bottom in 774 ranges, and rules `base_floor 2 / similarity 1161 /
  type_cap 15` (the sampled tail stays 69 / 17 and `height_report --check`
  passes). A forced `base_floor` height is exempt from the report's type-ceiling
  and slenderness checks, since the building's own base set it.
- Build stage ~1.8× faster on the cached 4×4 km sample. Each building shell is
  now extruded with shared ring vertices (no duplicate wall vertices, no vertex
  merge) and measured in one sorted-edge pass — closure, winding, volume,
  degenerate faces, finite coordinates — instead of `trimesh`'s cached
  properties; the generic `copy`/`update_faces`/`fix_winding`/`fix_normals`
  chain runs only for a shell the fast path cannot accept. The produced
  triangles are identical to `trimesh.creation.extrude_polygon`
  (`test_extruded_shell_matches_trimesh_polygon_construction`).
  `_contact_indices` uses one `STRtree.query(..., predicate="dwithin")` instead
  of one buffered query per footprint, and `TerrainGrid.min_over` samples the
  rings and the representative point in one `surface_z` call.
- The city STL is exported once. `fuse_city` keeps the bytes it already
  round-tripped to prove the file is closed, plus the mesh read back from them
  (`FusedCity.stl_bytes`/`stl_mesh`), and validates that file; `write_outputs`
  writes and classifies those bytes instead of exporting, reloading, and
  validating the city a second time. The pipeline also passes
  `check_input_volumes=False` to the boolean engine: every shell was validated
  before the union, and the fused mesh and the written file still are. Each
  accepted shell is censused once; the report that accepts it is the one
  `extrude_building` already measured, and only a repaired shell is measured
  again. Interleaved before/after on the cached 4×4 km sample (two runs per
  side): `build` 2.03 → 0.89 s, `fuse` 0.81 → 0.81 s, `export` 0.63 → 0.34 s,
  `total` 5.77 → 4.29 s; the written `city.stl` was byte-identical to the
  pre-refactor output (87 388 triangles, volume 1.33733e9 m³, 0 weld repairs,
  patches unchanged). The base-floor fix below adds two buildings, so the 2.2.0
  reference is 87 634 triangles.
- CI now has a `windows-latest` job next to the Ubuntu one — the GBK console bug
  was Windows-only and the docs are PowerShell-first — and both jobs carry
  `timeout-minutes`. `tests/golden/city_300m.json` records the versions that
  produced its metrics (`python`, `trimesh`, `manifold3d`, `pyproj`, `shapely`,
  `numpy`), and a golden mismatch prints both version sets with the changed
  names, so a dependency bump on an unchanged commit explains itself. A
  version-only change does not fail the test.
- The UI selection area uses the WGS84 ellipsoid now, the same datum the backend
  validates against: a 4×4 km selection reads 16.005 km² instead of the planar
  16.000 km², so the front end and `max_area_km2` agree. `map.js` computes the
  cell area in closed form; `tests/test_frontend_area.py` runs the shipped
  module under Node and compares it with `bbox_area_km2` on five boxes (the test
  skips when Node is not installed).
- `metadata.json → buildings.height_sampling` keeps one key set in every mode.
  When sampling is switched off or has too few tagged heights, `enabled: false`
  and `reason` (`disabled` / `too_few_known_heights`) sit beside zero counters,
  empty `rules`/`above_100m_by_group`, and the configured parameters, so a
  consumer never has to branch on which keys exist. The height report prints
  `disabled (<reason>)` for such a job.
- Decisions on the last two data-hygiene questions: very large low-rise OSM
  footprints (plazas, markets) are kept — they are real shapes, and the
  documented control (`min_building_area_m2`) only drops small buildings, so a
  new size filter would delete real city fabric; the optional OBJ with
  ground/wall groups is not added, because it would grow every job and ZIP while
  `patches.json` already carries the same grouping in STL order. Both are
  recorded in `docs/osm-to-stl.md`; an opt-in `--obj` flag remains the escape
  hatch.

- `patches.json` ground detection now tests the whole triangle and its normal:
  the centroid and all three vertices must lie within
  `CITYGEN_PATCH_GROUND_TOLERANCE_M` of the terrain surface, and the face normal
  must agree with the terrain normal (`CITYGEN_PATCH_GROUND_NORMAL_DOT_MIN`,
  0.9). On the 4×4 km sample 97 of 54 615 `ground` faces were wrong — 73
  near-surface roof/wall triangles with a vertex up to 0.34 m off the surface
  and 24 near-vertical slivers with |nz| ≈ 0.02 — so ground is now 54 518 and
  wall 32 322, with the geometry unchanged (87 388 triangles, same volume). A
  legitimate terrain triangle on any slope stays `ground`; the 1 km² sample has
  3 721 ground faces, every vertex within 0.08 mm of the surface and a normal
  dot of at least 0.996.
- The Three.js preview colours the fused city by patch group: it loads
  `city.stl` plus `patches.json`, splits the geometry by the recorded ranges
  into `ground` / `wall` / `bottom` meshes (ground and walls share the same
  warm-white material, the base underside is darker), and groups the ground and
  wall counts under one footer-legend swatch next to the bottom count. Jobs
  without a readable `patches.json` fall
  back to the single-material `preview.stl`; if the ranges load but do not cover
  the mesh, the viewer keeps `city.stl` in one material and warns in the
  console. The result panel gains a
  `地面 / 墙面` card, and `stats.patch_counts` / `stats.patch_ranges` expose the
  counts to any client. The README preview screenshot was regenerated with the
  shared ground/wall material.
- `patches.json` and the metadata `patches` block record
  `ground_normal_dot_min`.
- Height sampling v3: an untagged building now draws from observations whose own
  footprint is within `0.5–2×` its area (the draw falls back to the weighted pool
  of every observation with a known area when that band holds none), weighted by
  a log-area kernel and by building type (same
  `building` type ×2, same type group ×1.5) instead of a uniform draw inside the
  old `0.25–4×` band. OSM tags over-represent towers, so this keeps a 112 m²
  building from copying a 239 m tower. On the 4×4 km sample the sampled
  buildings above 250 m fall from 21 to 17 and above 100 m from 73 to 69 against
  a pool share of 2.6 % / 8.9 %; samples with height/√area > 10 fall from 12 to
  1 and the worst ratio from 17.0 to 10.9. Every sampled building records the
  rule that produced its height (`similarity`, `type_cap`, `conservative_cap`,
  and the 2.2.0 `base_floor`)
  as `height_rule` in `buildings.geojson`, and
  `metadata.json → buildings.height_sampling` gains the pool's and the sample's
  ≥100 m/≥250 m counts by type group (per OSM building; `sampled` still counts
  prepared parts and `sampled_buildings` the distinct buildings), the rule
  counts, `band_fallback_draws` (23 on the 4×4 km sample), `area_ratio`, and the
  weighting parameters (`area_bandwidth_log`, `type_factors`); `area_ratio` is
  now `[0.5, 2.0]`.
- Optional conservative height mode: `--conservative-heights` /
  `CITYGEN_HEIGHT_SAMPLING_CONSERVATIVE=true` caps sampled heights at
  `k·√(footprint area)` (`--slenderness-k`, default `8`). It never raises a
  height; on the 4×4 km sample it trims 7 more draws, leaving no sample above
  ratio 8 and 15 above 250 m.
- `python -m app.services.height_report --output <job> [--check]` prints the
  sampled tail against the tagged pool, the rule counts, and the most slender
  samples; `--check` fails when the sampled ≥250 m share exceeds the pool's or a
  sampled height exceeds its type ceiling.

## [2.1.1] - 2026-11-02

- Height sampling no longer copies censored or implausible observations.
  Heights clamped by the 2–300 m range (e.g. an OSM `height=350`) are excluded
  from the sampling pool, and low-rise/civic/industrial `building` types get a
  sampled ceiling (train station 60 m, school 35 m, museum 45 m, retail 30 m,
  warehouse 25 m, house 15 m, …), because OSM height tags over-represent towers.
  `metadata.json → buildings.height_sampling.type_capped` records how many
  samples hit a ceiling. On the 4×4 km sample: 15 buildings capped, sampled
  heights ≥250 m fall from 42 to 21 and ≥100 m from 94 to 73; the train station
  drops from 297 m to 60 m, the school from 245 m to 35 m, and a
  `building=house` sample from 289 m to 15 m.

## [2.1.0] - 2026-11-02

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
  2 000 m² vs 33.5 m above), where before a shed could draw a tower height. The
  changed heights move that sample to 84 654 triangles and 1.35378e9 m³ (from
  81 864 and 1.32476e9), with zero welds and a watertight surface.
- `patches.json` now ships next to `city.stl`: contiguous triangle ranges
  labelled `ground` (upward and within `CITYGEN_PATCH_GROUND_TOLERANCE_M` =
  0.05 m of the terrain surface), `wall` (building walls, roofs, and the
  vertical terrain skirt), or `bottom` (the base underside). Ranges follow the
  STL triangle order, so a solver can assign CFD boundary patches in one pass
  without splitting the closed surface. Metadata gains a `patches` block with
  the counts, tolerance, and range count; the file is listed in
  `metadata.json → files`, `result.zip`, and `README.txt`. On the 4×4 km sample:
  53 673 ground / 30 433 wall / 548 bottom triangles (910 ranges); on the
  0.74 km² sample: 10 420 / 22 554 / 146 (479 ranges).
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
  tuples per vertex (all 27 cells are still scanned, so cross-cell pairs cannot
  slip through); the non-manifold edge census packs each sorted edge into one
  integer key instead of sorting a structured array; and the fused mesh is
  validated once instead of twice (in `fuse_city` and again for the written
  STL). On the cached 4×4 km sample (1 696 buildings) `fuse` drops 4.41 → ≈2.4 s
  and the whole run 17.5 → ≈14 s, with identical triangles (81 864), volume
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
