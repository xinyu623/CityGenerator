"""Pin the preview's ground/wall material and the grouped footer legend.

The ground used to be a darker sand tone; the owner asked for it to match the
buildings, so the terrain and the walls now share one material and the legend
groups their counts under one swatch. This runs the shipped `viewer.js` under
Node and checks both, plus the fallback legend for a job without patches.
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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_preview_draws_the_ground_in_the_building_material(tmp_path):
    materials = _viewer_payload(tmp_path)["materials"]

    assert materials["ground"] == materials["wall"]
    assert materials["ground"]["color"] == 0xF2EBDC
    assert materials["ground"]["roughness"] == materials["wall"]["roughness"]
    assert materials["bottom"] != materials["ground"]


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
