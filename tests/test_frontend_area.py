"""Check the UI's selection area against the backend's WGS84 area.

The front end labels a selection with its planar ``width x height`` while the
backend validates ``max_area_km2`` on the ellipsoid, so a 4 x 4 km box read
16.000 km² in the UI and 16.005 km² in the API. ``map.js`` now computes the
same graticule-cell area the backend's pyproj polygon does; this test runs the
shipped module under Node and compares the two.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.models.schemas import BBox, GenerateRequest
from app.services.pipeline import bbox_area_km2

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]
MAP_JS = ROOT / "app" / "static" / "js" / "map.js"
FOUR_KM = (29.553, 106.546686, 29.589069, 106.588)

HARNESS = """
import { pathToFileURL } from "node:url";
const [mapPath, boxesJson] = process.argv.slice(2);
const module = await import(pathToFileURL(mapPath).href);
const boxes = JSON.parse(boxesJson);
const areas = boxes.map((box) => module.geodesicAreaKm2(box[0], box[1], box[2], box[3]));
const selection = module.selectionFromBounds({
  getSouth: () => boxes[0][0],
  getWest: () => boxes[0][1],
  getNorth: () => boxes[0][2],
  getEast: () => boxes[0][3],
});
console.log(JSON.stringify({ areas, selection }));
"""


def _js_area(tmp_path: Path, boxes: list[tuple[float, float, float, float]]) -> dict:
    harness = tmp_path / "area_harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    completed = subprocess.run(
        [NODE, str(harness), str(MAP_JS), json.dumps(boxes)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _backend_area(box: tuple[float, float, float, float]) -> float:
    south, west, north, east = box
    request = GenerateRequest(bbox=BBox(south=south, west=west, north=north, east=east))
    return bbox_area_km2(request)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_selection_area_matches_the_backend_ellipsoid_area(tmp_path):
    boxes = [
        FOUR_KM,
        (28.20, 112.90, 28.21, 112.91),
        (-33.90, 151.20, -33.89, 151.21),
        (60.00, 10.00, 60.01, 10.01),
        (-0.01, -0.01, 0.01, 0.01),
    ]

    payload = _js_area(tmp_path, boxes)

    assert payload["areas"] == pytest.approx([_backend_area(box) for box in boxes], rel=1e-6)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_four_km_selection_reads_as_the_backend_area(tmp_path):
    payload = _js_area(tmp_path, [FOUR_KM])
    selection = payload["selection"]

    assert _backend_area(FOUR_KM) == pytest.approx(16.005367, abs=1e-6)
    assert round(selection["areaKm2"], 3) == 16.005
    assert selection["width"] == pytest.approx(3996.245, abs=0.01)
    assert selection["height"] == pytest.approx(4010.695, abs=0.01)
    assert selection["areaKm2"] != pytest.approx(selection["width"] * selection["height"] / 1e6)
