from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from app.cli import main, parse_bbox
from app.services.errors import OverpassError
from app.services.overpass import OverpassResult

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "overpass_tiny.json").read_text(encoding="utf-8")
)
BBOX = "28.20,112.90,28.21,112.91"


def _fake_fetch(raw: dict):
    def fetch(self, bbox, *, refresh=False, progress_cb=None):
        if progress_cb is not None:
            progress_cb(1, 1)
        return OverpassResult(
            raw=raw,
            meta={
                "query_version": "test",
                "query_hash": "test-hash",
                "endpoint": "https://overpass.example/api",
                "fetched_at": "2026-10-29T00:00:00+00:00",
                "osm_base_timestamp": "2026-10-29T00:00:00Z",
            },
            from_cache=False,
        )

    return fetch


def test_parse_bbox_accepts_four_numbers():
    bbox = parse_bbox(" 28.20, 112.90 ,28.21,112.91 ")
    assert bbox.south == pytest.approx(28.20)
    assert bbox.east == pytest.approx(112.91)


@pytest.mark.parametrize("value", ["1,2,3", "1,2,3,4,5", "a,b,c,d", "28.21,112.90,28.20,112.91"])
def test_parse_bbox_rejects_bad_input(value):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_bbox(value)


def test_cli_generates_a_flat_city(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("app.services.pipeline.OverpassClient.fetch", _fake_fetch(FIXTURE))
    out = tmp_path / "job"
    code = main(
        [
            "--bbox",
            BBOX,
            "--out",
            str(out),
            "--no-terrain",
            "--data-dir",
            str(tmp_path / "data"),
        ]
    )

    assert code == 0
    assert (out / "output" / "city.stl").is_file()
    assert (out / "output" / "metadata.json").is_file()
    assert (out / "output" / "result.zip").is_file()
    captured = capsys.readouterr().out
    assert "buildings" in captured
    assert "watertight" in captured


def test_cli_reports_a_pipeline_error(tmp_path, monkeypatch, capsys):
    def failing_fetch(self, bbox, *, refresh=False, progress_cb=None):
        raise OverpassError("Overpass 不可用", "all endpoints failed")

    monkeypatch.setattr("app.services.pipeline.OverpassClient.fetch", failing_fetch)
    code = main(
        [
            "--bbox",
            BBOX,
            "--out",
            str(tmp_path / "job"),
            "--no-terrain",
            "--data-dir",
            str(tmp_path / "data"),
        ]
    )

    assert code == 1
    assert "Overpass 不可用" in capsys.readouterr().err
