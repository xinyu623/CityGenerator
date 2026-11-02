# patches.json on steep terrain + viewer colours Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `patches.json` label ground only when a triangle really lies on the terrain (not just its centroid), and colour `ground`/`wall` in the Three.js preview from `patches.json`, including the patch counts in the result stats.

**Architecture:** `app/services/patches.py` keeps owning the classification; `TerrainGrid` gains the exact TIN normal it needs. `app/services/output.py` passes the new `normal_dot_min` setting into the classifier and records it in `patches.json`/`metadata.json`. The preview loads `city.stl` + `patches.json` instead of `preview.stl` when patches exist, splits the geometry by the recorded ranges in the browser, and gives the two (three, with `bottom`) kinds their own materials; the pipeline stats carry the patch counts so the sidebar can show them.

**Tech Stack:** Python 3.11, numpy, shapely, trimesh, pytest, ruff; Three.js (ES modules), plain DOM.

**Spec:** `docs/plans/2026-11-02-post-v2.1.1-handoff.md` task 2.

## Global Constraints

- `patches.json` ranges must still cover every triangle exactly once, in STL order; the three kinds stay `ground`, `wall`, `bottom`.
- Tagged/known heights, sampling, and geometry must not change: only the classification of a handful of near-surface triangles.
- Flat-ground runs (terrain off) keep working: ground is the slab top, the underside is `bottom`.
- The preview must keep working for old jobs without `patches.json` (fall back to `preview.stl`, one material).
- Offline tests only (`pytest tests -q`), `ruff check app tests` clean.

## Review Focus

- A TIN ground triangle on a 40° slope must stay `ground` (its normal is the terrain normal, its vertices are on the surface) — the guards must not reject legitimate terrain.
- A low building's roof triangle that is within 5 cm of the terrain at its centroid but tilted or raised at a vertex must become `wall`.
- A near-vertical wall sliver whose vertices all sit within 5 cm of the ground plane must not be labelled `ground` (flat terrain included).
- The viewer split must handle jobs whose `patches.json` is missing, and must not mis-split when a job's `city.stl` differs from the ranges' triangle count.
- The UI must show both colours, and the stats must show the patch counts (a job generated before this change must still render).

---

### Task 1: Terrain normal + whole-triangle ground guard

**Files:**
- Modify: `app/services/terrain.py`, `app/services/patches.py`, `app/services/output.py`, `app/config.py`
- Test: `tests/test_terrain.py`, `tests/test_patches.py`

**Interfaces:**
- Consumes: `TerrainGrid.surface_z` (existing).
- Produces: `TerrainGrid.surface_normal(x, y) -> np.ndarray` (shape `(..., 3)`, unit, +z);
  `face_kinds(mesh, *, terrain=None, ground_tolerance_m=0.05, normal_dot_min=0.9)`;
  `Settings.patch_ground_normal_dot_min`; `patches.json` and metadata `patches`
  gain `ground_normal_dot_min`.

- [ ] **Step 1: Write the failing tests**

```python
def test_terrain_surface_normal_follows_the_ramp():
    grid = TerrainGrid(heights=np.array([[0.0, 1.0], [0.0, 1.0]]), x0=0.0, y0=0.0, dx=1.0, dy=1.0)
    normal = grid.surface_normal(np.array([0.5]), np.array([0.5]))
    assert normal[0] == pytest.approx([-1.0, 0.0, 1.0] / np.sqrt(2.0))


def test_ground_face_with_a_vertex_off_the_surface_is_a_wall():
    # centroid 0.03 m above z=0, one vertex 0.06 m up: not a ground triangle.
    mesh = _mesh(
        [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.09)],  # centroid z = 0.03
        [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (0.0, 4.0, 0.0)],  # flat ground
    )
    kinds = face_kinds(mesh, ground_tolerance_m=0.05)
    assert list(kinds) == [WALL, GROUND]


def test_near_vertical_sliver_on_the_ground_plane_is_a_wall():
    mesh = _mesh([(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 1.0, 0.3)])
    assert face_kinds(mesh, ground_tolerance_m=0.05)[0] == WALL
```

`_mesh` is a small helper building a non-indexed `trimesh.Trimesh` from triangle triples.

- [ ] **Step 2: Run them, watch them fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_terrain.py tests/test_patches.py -q -k "normal or surface or sliver"`
Expected: FAIL — `surface_normal` missing, the tilted triangle still `ground`.

- [ ] **Step 3: Implement**

`surface_normal` mirrors `surface_z`'s cell split and returns the plane normal from the
gradient. `face_kinds` requires, for an upward non-bottom face: centroid within tolerance,
**all three vertices** within tolerance (evaluated at each vertex's x/y), and when
`terrain` is given `dot(face_normal, terrain_normal) >= normal_dot_min` (for flat ground:
`nz >= normal_dot_min`). Add the setting and record it in `patches.json` and metadata.

- [ ] **Step 4: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_terrain.py tests/test_patches.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/services/terrain.py app/services/patches.py app/services/output.py app/config.py tests
git commit -m "fix: label ground only when the whole triangle lies on the terrain"
```

### Task 2: Pipeline stats carry the patch counts

**Files:**
- Modify: `app/services/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `stats["patch_counts"]` = `{"ground": int, "wall": int, "bottom": int}` and
  `stats["patch_ranges"]` = int.

- [ ] **Step 1: Write the failing test**

```python
def test_pipeline_stats_include_the_patch_counts(...):
    ...
    assert result.stats["patch_counts"] == metadata["patches"]["counts"]
    assert result.stats["patch_ranges"] == metadata["patches"]["range_count"]
```

- [ ] **Step 2: Run it, watch it fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_pipeline.py -q -k patch_counts`
Expected: FAIL — `KeyError: 'patch_counts'`.

- [ ] **Step 3: Implement** — read the counts from `bundle.metadata["patches"]` in
`run_pipeline` and add them to `stats`.

- [ ] **Step 4: Run the tests** — `.\.venv\Scripts\python.exe -m pytest tests/test_pipeline.py -q` → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat: expose the patch counts in the job stats"`

### Task 3: Colour the preview from patches.json

**Files:**
- Modify: `app/static/js/viewer.js`, `app/static/js/app.js`, `app/static/index.html`
- Test: none (UI), verified by the screenshot in task 5.

**Interfaces:**
- Consumes: `job.files` (names + urls), `patches.json` (`ranges`, `counts`).
- Produces: `openViewer({ urls, patches, meta })` where `patches` is
  `{ ranges: [{start, count, kind}], counts }` or `null`; the viewer splits the loaded
  city geometry into `ground`/`wall`/`bottom` meshes and shows a legend.

- [ ] **Step 1: Extend the preview entry point** — `app.js` `preview()` finds
`city.stl` and `patches.json` in `job.files`, fetches the JSON, and calls
`openViewer({ urls: [{ url: cityUrl, kind: "city" }], patches, meta })`; falls back to
`/api/jobs/<id>/preview.stl` when `patches.json` is absent.

- [ ] **Step 2: Split the geometry** in `viewer.js`: after loading and `rotateX`, slice the
non-indexed position/normal arrays by the contiguous ranges into one geometry per kind
(drop nothing; `bottom` uses its own material), then run the existing clean/translate
loop per part. Guard: when the range cover does not equal the triangle count, log to
`console.warn` and keep a single city mesh.

- [ ] **Step 3: Materials + legend** — add `wall` and `bottom` materials; a small legend
in the viewer modal (two/three coloured dots + labels) filled from the kinds present.

- [ ] **Step 4: Stats** — add a `地面 / 墙面` stat card from `job.stats.patch_counts`.

- [ ] **Step 5: Update the sidebar hint** (`index.html`) to mention `patches.json` and the
preview colours instead of "需要分 patch 时请在前处理软件中处理".

- [ ] **Step 6: Commit** — `git commit -m "feat: colour the 3D preview by patch group"`

### Task 4: 4×4 + 1 km² verification, docs, CHANGELOG

**Files:**
- Modify: `docs/osm-to-stl.md`, `docs/architecture.md`, `README.md`, `README.zh-CN.md`,
  `CHANGELOG.md`, `docs/plans/2026-11-02-post-v2.1.1-handoff.md`

- [ ] **Step 1: Rerun the 4×4 box** and compare the patch counts with
`artifacts/height-sampling-v3/output`; confirm `ranges` still cover 100 % and the
geometry numbers are unchanged.
- [ ] **Step 2: Run a ~1 km² box** and read the classification (`height_report` is for
heights; use a short script or the metadata counts plus a render).
- [ ] **Step 3: Update the docs** (classifier rule, new setting, `patches.json` field,
viewer colours, stats).
- [ ] **Step 4: Full suite + ruff** — `.\.venv\Scripts\python.exe -m pytest tests` and
`ruff check app tests`.
- [ ] **Step 5: Commit** — `git commit -m "docs: patches on steep terrain and preview colours"`

### Task 5: README images

**Files:**
- Modify: `docs/images/frontend.png`, `docs/images/preview.png`

- [ ] **Step 1: Start the UI server** at `127.0.0.1:8000` (no `--reload`), as a managed
background job; keep it running for the screenshots and stop it afterwards.
- [ ] **Step 2: Generate a cached job** for the small Changsha box through the UI (select
two corners, generate) so the result panel and the viewer show a real job.
- [ ] **Step 3: Capture `frontend.png`** (1440×1000, full page) after the job completes.
- [ ] **Step 4: Open the 3D preview** and capture `preview.png` with the new patch colours
and legend.
- [ ] **Step 5: Commit** — `git commit -m "docs: refresh the README screenshots"`
