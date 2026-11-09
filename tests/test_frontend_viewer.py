"""Pin the preview's graphite materials, height ramp and grouped footer legend.

The 3D preview used to be one warm-white surface; it is now a graphite model
whose colours come from a height ramp, with the underside kept flat. This runs
the shipped `viewer.js` under Node and checks the material/look settings, the
ramp stops and the ramp's degenerate (flat selection) case, plus the grouped
legend for the footer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]
VIEWER_JS = ROOT / "app" / "static" / "js" / "viewer.js"

HARNESS = """
import { pathToFileURL } from "node:url";
const [viewerPath] = process.argv.slice(2);
const module = await import(pathToFileURL(viewerPath).href);
console.log(JSON.stringify({
  materials: module.MATERIALS,
  colors: module.COLORS,
  ramp: module.HEIGHT_RAMP,
  at0: module.rampColor(0),
  at1: module.rampColor(1),
  flat: Array.from(module.rampColors(new Float32Array([0, 0, 0, 0, 0, 0]), 0, 0)),
  ramp3: Array.from(module.rampColors(new Float32Array([0, 0, 0, 0, 5, 0, 0, 10, 0]), 0, 10)),
  mid: module.rampColor(0.5),
  interior: module.rampColor(0.3),
  mixed: module.legendGroups({ ground: 19718, wall: 12552, bottom: 322 }),
  groundOnly: module.legendGroups({ ground: 10 }),
  bottomOnly: module.legendGroups({ bottom: 5 }),
  empty: module.legendGroups(null),
}));
"""


def _viewer_payload(tmp_path: Path) -> dict:
    harness = tmp_path / "viewer_harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    completed = subprocess.run(
        [NODE, str(harness), str(VIEWER_JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def lum(rgb):
    """Perceptual luminance of a linear-space rgb triple."""
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _to_linear(hex_value: str) -> float:
    """One sRGB channel given as two hex digits ("DC") → linear 0–1."""
    c = int(hex_value, 16) / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _expected_stop(ramp, t: float) -> list[float]:
    """Evaluate the ramp at t in linear space, independently of viewer.js."""
    stops = [
        (stop_t, [_to_linear(channel) for channel in (h[1:3], h[3:5], h[5:7])])
        for stop_t, h in ramp
    ]
    for (t1, c1), (t0, c0) in zip(stops[1:], stops, strict=False):
        if t <= t1:
            k = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return [c0[j] + (c1[j] - c0[j]) * k for j in range(3)]
    return stops[-1][1]


VIEWER_SOURCE = VIEWER_JS.read_text(encoding="utf-8")


def test_the_height_ramp_is_wired_into_the_loaded_meshes():
    # Losing either half of the wiring leaves vertexColors materials without a
    # colour attribute, and three.js then renders the model black while every
    # other test still passes.
    assert "applyRamp(state, child.geometry, box.min.y, box.max.y)" in VIEWER_SOURCE
    assert "child.material.vertexColors !== true" in VIEWER_SOURCE
    # The gamma itself is a tuning value left to the implementer; this only
    # pins that the ramp is fed from the position attribute.
    assert "rampColors(position.array, minY, maxY" in VIEWER_SOURCE


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_preview_materials_are_graphite_with_vertex_colours(tmp_path):
    payload = _viewer_payload(tmp_path)
    materials = payload["materials"]
    assert materials["ground"] == materials["wall"] == materials["buildings"]
    assert materials["ground"]["color"] == 0xFFFFFF
    assert materials["ground"]["vertexColors"] is True
    assert materials["bottom"]["vertexColors"] is False
    assert materials["bottom"]["color"] == 0x6E675F
    # The no-patches fallback renders through MATERIALS.city alone; it must take
    # the same ramp, otherwise that path paints an all-white or all-black model.
    assert materials["city"]["vertexColors"] is True
    assert materials["city"]["color"] == 0xFFFFFF
    assert payload["colors"]["plate"] == 0xF2EFE9


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_height_ramp_runs_from_paper_grey_to_dark_graphite(tmp_path):
    payload = _viewer_payload(tmp_path)
    assert [stop[1] for stop in payload["ramp"]] == [
        "#E7E2D9", "#D9D2C8", "#C4BBAF", "#A99F93", "#8A8176", "#6E655B"
    ]
    assert [stop[0] for stop in payload["ramp"]] == [0.0, 0.1, 0.25, 0.45, 0.7, 1.0]
    assert lum(payload["at0"]) > lum(payload["at1"]) * 4


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_ramp_degrades_to_the_light_stop_on_a_flat_selection(tmp_path):
    payload = _viewer_payload(tmp_path)
    light = _expected_stop(payload["ramp"], 0.0)

    # A flat selection has no height range to normalise against: every vertex
    # takes the lightest stop, so no NaN and no black.
    assert payload["flat"] == pytest.approx(light * 2, rel=1e-5)

    ramp3 = payload["ramp3"]
    assert len(ramp3) == 9
    assert ramp3[:3] == pytest.approx(light, rel=1e-5)
    assert ramp3[6:] == pytest.approx(_expected_stop(payload["ramp"], 1.0), rel=1e-5)

    # The middle sample vertex sits at half height: it must land on the ramp's
    # interpolated value, computed here from the stops rather than from
    # rampColor, which would only compare the implementation to itself.
    assert ramp3[3:6] == pytest.approx(_expected_stop(payload["ramp"], 0.5), rel=1e-5)

    # A second interior sample on a different segment (0.25–0.45), so dropping
    # between-stop interpolation fails on two separate segments.
    assert payload["interior"] == pytest.approx(
        _expected_stop(payload["ramp"], 0.3), rel=1e-5
    )

    assert lum(payload["at0"]) > lum(payload["mid"]) > lum(payload["at1"])


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_legend_groups_the_ground_and_wall_counts(tmp_path):
    payload = _viewer_payload(tmp_path)

    assert payload["mixed"] == [
        {"swatch": "building", "label": "地面 19,718 · 建筑 12,552"},
        {"swatch": "bottom", "label": "底面 322"},
    ]
    assert payload["groundOnly"] == [{"swatch": "building", "label": "地面 10"}]
    assert payload["bottomOnly"] == [{"swatch": "bottom", "label": "底面 5"}]
    assert payload["empty"] == [{"swatch": "building", "label": "城市实体"}]
