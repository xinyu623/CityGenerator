# Height sampling v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hard area band in the height sampler with type-and-area similarity weighting, add an opt-in conservative slenderness cap, and record which rule produced every sampled height.

**Architecture:** `app/services/heights.py` owns the pool, the weights, the type ceilings, and the sampling decision; `prepare_buildings` only counts the rules and writes the metadata block. The conservative mode is a settings/CLI switch (`height <= k * sqrt(area)`, `k = 8`), off by default. `scripts/height_report.py` validates a finished job without rerunning the pipeline.

**Tech Stack:** Python 3.11, pytest, ruff, pydantic-settings, shapely, existing `app.services` modules.

**Spec:** `docs/plans/2026-11-02-post-v2.1.1-handoff.md` task 1, with the approach picked after the 4×4 km pool analysis (option B: weighted similarity).

## Global Constraints

- Every sampled height stays inside `2.0 <= h <= 300.0` and keeps the existing `SAMPLED_HEIGHT_CAPS_M` type ceilings.
- Sampling stays deterministic: same `height_sampling_seed` and same input order give identical heights.
- `height_sampling_enabled = false` and `< height_sampling_min_samples` keep falling back to `default_height_m`.
- Tagged heights (`tag`, `levels`) are never sampled, capped, or weighted.
- Tests must not touch the network (`pytest tests -q`, 3 network tests skip).
- `ruff check app tests` stays clean; line length 100.

## Review Focus

- A small footprint (`~700 m²`) near a tower observation must not inherit the tower height; the area kernel is the only thing preventing it — test with a pool of similar small buildings plus one 250 m tower.
- Conservative mode must never raise a height and must be inert when the footprint area is unknown or zero.
- A pool whose areas are all missing (plain float observations) must still sample uniformly instead of raising.
- `metadata.buildings.height_sampling` must keep its existing keys (`enabled`, `known`, `sampled`, `type_capped`) so older consumers and the UI keep working.
- Samples above the type ceiling must not appear even when the conservative bound is also active.

---

### Task 1: Weighted sampler with rules

**Files:**
- Modify: `app/services/heights.py`
- Test: `tests/test_heights.py`

**Interfaces:**
- Consumes: `resolve_height`, `SAMPLED_HEIGHT_CAPS_M`, `sampled_height_cap` (existing).
- Produces:
  - `building_type_group(building_type: str | None) -> str` — one of `"residential"`, `"commercial"`, `"civic"`, `"industrial"`, `"other"`, `"unspecified"`.
  - `KnownHeight(height_m: float, area_m2: float = 0.0, building_type: str = "")` — frozen dataclass.
  - `SampledHeight(height_m: float, rule: str)` — frozen dataclass; `rule` in `{"similarity", "pool", "type_cap", "conservative_cap"}`.
  - `HeightSampler(known, *, jitter=0.1, seed=0, conservative=False, slenderness_k=8.0)`; `draw(area_m2=None, building_type=None) -> SampledHeight`.
  - Weight constants `TYPE_WEIGHT = 2.0`, `GROUP_WEIGHT = 1.5`, `AREA_BANDWIDTH_LOG = 0.7`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize(
    ("building_type", "group"),
    [
        ("yes", "unspecified"),
        (None, "unspecified"),
        ("apartments", "residential"),
        ("retail", "commercial"),
        ("school", "civic"),
        ("warehouse", "industrial"),
        ("tower", "other"),
    ],
)
def test_building_type_group_maps_osm_values(building_type, group):
    assert building_type_group(building_type) == group


def test_height_sampler_prefers_the_same_type_and_area():
    # One 700 m2 walk-up, twenty similar walk-ups, one 250 m tower at 1700 m2.
    known = [KnownHeight(9.0, 700.0, "yes") for _ in range(20)]
    known.append(KnownHeight(250.0, 1700.0, "yes"))
    sampler = HeightSampler(known, jitter=0.0, seed=0)

    draws = [sampler.draw(700.0, "yes") for _ in range(200)]

    tall = [draw for draw in draws if draw.height_m >= 100.0]
    assert len(tall) <= 2
    assert {draw.rule for draw in draws} == {"similarity"}


def test_height_sampler_records_the_rule():
    sampler = HeightSampler([KnownHeight(9.0, 700.0, "house")], jitter=0.0, seed=0)

    assert sampler.draw(700.0, "house").rule == "similarity"
    assert sampler.draw(None, "house").rule == "pool"
    assert sampler.draw(700.0, "train_station").rule == "type_cap"


def test_height_sampler_applies_the_conservative_slenderness_bound():
    sampler = HeightSampler(
        [KnownHeight(200.0, 400.0, "yes")],
        jitter=0.0,
        seed=0,
        conservative=True,
        slenderness_k=8.0,
    )

    draw = sampler.draw(400.0, "yes")

    assert draw.height_m == pytest.approx(160.0)
    assert draw.rule == "conservative_cap"


def test_conservative_bound_is_inactive_without_an_area():
    sampler = HeightSampler(
        [KnownHeight(200.0, 400.0, "yes")],
        jitter=0.0,
        seed=0,
        conservative=True,
    )

    assert sampler.draw(None, "yes").height_m == pytest.approx(200.0)
    assert sampler.draw(0.0, "yes").height_m == pytest.approx(200.0)
```

Update the existing tests that unpacked a float from `draw()` to use `.height_m`, and
`test_height_sampler_prefers_heights_from_similar_footprints` to assert the weighted
outcome (the 4 m observation dominates 50 draws for a 30 m² target, no 100 m draw).

- [ ] **Step 2: Run the new tests, watch them fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_heights.py -q`
Expected: FAIL — `building_type_group`/`KnownHeight`/`SampledHeight` not importable, `draw()` returns a float.

- [ ] **Step 3: Implement the sampler in `app/services/heights.py`**

Weights: `exp(-0.5 * (ln(obs_area / target_area) / AREA_BANDWIDTH_LOG) ** 2)`, times
`TYPE_WEIGHT` when the building types match exactly (neither is `unspecified`), else
`GROUP_WEIGHT` when the type groups match, else `1.0`. Draw with
`self._rng.choices(indices, weights=..., k=1)`. Fall back to uniform over the whole
pool (rule `"pool"`) when the target area is missing/zero, no observation has an area,
or every weight is zero. Apply `sampled_height_cap(type)` first (rule `"type_cap"`),
then, when `conservative` and the target area is positive, `slenderness_k * sqrt(area)`
(rule `"conservative_cap"`); jitter after the draw, then clamp to `[2.0, 300.0]`.

- [ ] **Step 4: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_heights.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/services/heights.py tests/test_heights.py
git commit -m "feat: weight height samples by type and area similarity"
```

### Task 2: Wire the sampler into prepare_buildings

**Files:**
- Modify: `app/services/buildings.py`, `app/services/pipeline.py`
- Test: `tests/test_geometry.py`

**Interfaces:**
- Consumes: `KnownHeight`, `SampledHeight`, `HeightSampler`, `building_type_group` from task 1.
- Produces: `PreparedBuilding.height_rule: str` and `BuildingRecord.height_rule: str`;
  `prepare_buildings(..., height_conservative: bool = False, height_slenderness_k: float = 8.0)`;
  `PreparedBuildings.height_sampling` with the keys listed in the Review Focus.

- [ ] **Step 1: Write the failing tests**

```python
def test_sampled_buildings_record_the_rule_that_produced_their_height():
    projector = projector_for_bbox(28.20, 112.90, 28.21, 112.91)
    features = [
        _feature(1, {"building": "house", "height": "6"}, 0.000),
        _feature(2, {"building": "house"}, 0.002),
    ]
    prepared = prepare_buildings(features, projector, min_height_samples=1, height_seed=3)

    house = next(item for item in prepared.items if item.osm_id == 2)
    assert house.height_source == "sampled"
    assert house.height_rule == "similarity"
    sampling = prepared.height_sampling
    assert sampling["rules"]["similarity"] == 1
    assert sampling["weighting"] == "type_and_area_similarity"
    assert sampling["sampled_above_100m"] == 0
    assert sampling["pool_above_100m"] == 0
    assert "above_100m_by_group" in sampling


def test_conservative_sampling_is_opt_in():
    projector = projector_for_bbox(28.20, 112.90, 28.21, 112.91)
    features = [
        _feature(1, {"building": "yes", "height": "250"}, 0.000),
        _feature(2, {"building": "yes"}, 0.002, size=0.0004),
    ]
    loose = prepare_buildings(features, projector, min_height_samples=1, height_seed=5)
    tight = prepare_buildings(
        features,
        projector,
        min_height_samples=1,
        height_seed=5,
        height_conservative=True,
        height_slenderness_k=8.0,
    )

    tight_building = next(item for item in tight.items if item.osm_id == 2)
    assert tight_building.height_rule == "conservative_cap"
    assert tight_building.height_m < 250.0
    assert tight.height_sampling["conservative"] is True
    assert loose.height_sampling["conservative"] is False
```

Update `test_clamped_heights_are_not_sampling_observations` to read
`KnownHeight.height_m`, and `test_sampled_heights_respect_low_rise_type_caps` to assert
`prepared.height_sampling["rules"]["type_cap"] == 1`.

- [ ] **Step 2: Run the new tests, watch them fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_geometry.py -q -k "rule or conservative"`
Expected: FAIL — `height_rule` is not a field.

- [ ] **Step 3: Implement**

`_known_height_pairs` returns `list[KnownHeight]` (height, raw WGS84 area, `building`
tag). `prepare_buildings` builds the sampler with `conservative`/`slenderness_k`,
counts `rules`, `sampled_above_100m`, `sampled_above_250m`, `above_100m_by_group`,
plus the same three statistics for the pool (`known` observations) and
`weighting`/`type_factors`/`area_bandwidth_log`/`conservative`/`slenderness_k`.
Pass the two new kwargs through `run_pipeline` from settings.

- [ ] **Step 4: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_geometry.py tests/test_pipeline.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/services/buildings.py app/services/pipeline.py tests/test_geometry.py
git commit -m "feat: record the height-sampling rule and tail statistics"
```

### Task 3: Settings, CLI, and output

**Files:**
- Modify: `app/config.py`, `app/cli.py`, `app/services/output.py`, `app/services/pipeline.py`
- Test: `tests/test_cli.py`, `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `prepare_buildings` kwargs from task 2.
- Produces: `Settings.height_sampling_conservative: bool = False`,
  `Settings.height_sampling_slenderness_k: float = 8.0`; CLI flags
  `--conservative-heights` and `--slenderness-k`; `buildings.geojson` properties gain
  `height_rule` for sampled buildings.

- [ ] **Step 1: Write the failing tests**

```python
def test_cli_parser_accepts_the_conservative_height_flags():
    args = build_parser().parse_args(
        ["--bbox", "1,2,3,4", "--out", "x", "--conservative-heights", "--slenderness-k", "6"]
    )
    assert args.conservative_heights is True
    assert args.slenderness_k == 6.0


def test_geojson_marks_the_sampling_rule(tmp_path):
    # build_result with one sampled record -> feature properties["height_rule"]
    ...
```

- [ ] **Step 2: Run them, watch them fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli.py -q`
Expected: FAIL — unrecognised arguments.

- [ ] **Step 3: Implement**

`Settings` gains the two fields; `cli.main` passes `settings.height_sampling_conservative`
(overridden by the flag) and `slenderness_k` into `run_pipeline`; `run_pipeline` forwards
them; `_selected_tags`/`_geojson` emit `height_rule` when non-empty.

- [ ] **Step 4: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli.py tests/test_pipeline.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/config.py app/cli.py app/services/output.py app/services/pipeline.py tests
git commit -m "feat: expose conservative height sampling on the CLI"
```

### Task 4: 4×4 validation script

**Files:**
- Create: `scripts/height_report.py`
- Test: `tests/test_height_report.py`

**Interfaces:**
- Produces: `summarize(metadata: dict, features: list[dict]) -> dict` (pool and sampled
  tail shares, per-group medians, rule counts, worst slenderness cases),
  `format_report(summary: dict) -> str`, `check(summary: dict) -> list[str]`
  (empty when the acceptance invariants hold), and a `main()` with
  `--output DIR` / `--check`.

- [ ] **Step 1: Write the failing test**

```python
def test_height_report_flags_a_sampled_tail_above_the_pool_share(tmp_path):
    metadata = {...  # sampled_above_250m 3 of 10, pool_above_250m 0 of 10
    }
    summary = summarize(metadata, features)

    assert summary["sampled"]["above_250m_share"] == pytest.approx(0.3)
    assert any("250" in line for line in check(summary))
```

- [ ] **Step 2: Run it, watch it fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_height_report.py -q`
Expected: FAIL — no module.

- [ ] **Step 3: Implement the script and its report**

- [ ] **Step 4: Run the tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_height_report.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add scripts/height_report.py tests/test_height_report.py
git commit -m "feat: add a height-tail validation report"
```

### Task 5: 4×4 km rerun, visual check, docs, CHANGELOG

**Files:**
- Modify: `docs/osm-to-stl.md`, `docs/architecture.md` (if it documents sampling),
  `README.md`, `README.zh-CN.md`, `CHANGELOG.md`,
  `docs/plans/2026-11-02-post-v2.1.1-handoff.md`
- Test: none (documentation), plus the rerun below.

- [ ] **Step 1: Rerun the reference box from cache**

```powershell
.\.venv\Scripts\python.exe -m app.cli --bbox 29.553,106.546686,29.589069,106.588 --out .\scratch\h3 --data-dir data
```

- [ ] **Step 2: Compare and render**

```powershell
.\.venv\Scripts\python.exe scripts\height_report.py --output scratch\h3\output --check
.\.venv\Scripts\python.exe scratch\render_city.py scratch\h3 --output scratch\h3\preview.png
```

Record sampled ≥100 m / ≥250 m versus the pool, the rule counts, triangle and volume
deltas against `artifacts/height-fix-chongqing/output`, and look at the PNG next to
`artifacts/height-fix-chongqing/output/preview.png`.

- [ ] **Step 3: Update the docs, CHANGELOG `[Unreleased]`, and the handoff**

- [ ] **Step 4: Run the full suite and ruff**

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m ruff check app tests scripts
```

Expected: all offline tests pass, ruff clean.

- [ ] **Step 5: Commit**

```powershell
git add docs README.md README.zh-CN.md CHANGELOG.md
git commit -m "docs: height sampling v3"
```
