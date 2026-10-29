from __future__ import annotations

import json
import threading
import zipfile
from pathlib import Path

import pytest

from app.config import Settings
from app.models.schemas import BBox, GenerateOptions, GenerateRequest
from app.services.errors import AreaTooLargeError, NoBuildingsError
from app.services.overpass import OverpassResult
from app.services.pipeline import run_pipeline
from app.services.stl_export import load_stl_bytes

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "overpass_tiny.json"
RAW = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
BBOX = BBox(south=28.20, west=112.90, north=28.21, east=112.91)


def fake_fetch_factory(raw: dict):
    def fake_fetch(self, bbox, *, refresh=False):
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

    return fake_fetch


def test_pipeline_generates_expected_files(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.pipeline.OverpassClient.fetch",
        fake_fetch_factory(RAW),
    )
    settings = Settings(data_dir=tmp_path, max_area_km2=4.0)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    progress: list[tuple[int, str, str]] = []

    result = run_pipeline(
        GenerateRequest(bbox=BBOX, options=GenerateOptions()),
        settings,
        job_dir,
        progress_cb=lambda percent, stage, message: progress.append((percent, stage, message)),
        cancel_event=threading.Event(),
    )

    assert result.zip_path.exists()
    assert result.stats["buildings"] == 3
    assert result.stats["triangles"] > 0
    assert result.stats["height_sources"]["tag"] == 1
    assert result.stats["height_sources"]["levels"] == 2
    assert progress[-1][0] == 100

    with zipfile.ZipFile(result.zip_path) as archive:
        names = set(archive.namelist())
    assert {"buildings.stl", "ground.stl", "city.stl", "metadata.json", "README.txt"} <= names
    assert "buildings.geojson" in names

    metadata = json.loads((job_dir / "output" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["buildings"]["count"] == 3
    assert metadata["crs_projected"] == "EPSG:32649"
    assert metadata["local_frame"]["z"] == "up"
    assert metadata["ground"]["included"] is True
    assert metadata["attribution"].startswith("© OpenStreetMap")
    assert metadata["buildings"]["each_shell_watertight"] is True
    assert metadata["buildings"]["non_manifold_edges"] == 0

    buildings = load_stl_bytes((job_dir / "output" / "buildings.stl").read_bytes())
    assert buildings.is_watertight
    assert len(buildings.faces) > 0

    city = load_stl_bytes((job_dir / "output" / "city.stl").read_bytes())
    assert len(city.faces) > len(buildings.faces)


def test_pipeline_rejects_oversized_area(tmp_path, monkeypatch):
    called = {"fetch": False}

    def fake_fetch(self, bbox, *, refresh=False):
        called["fetch"] = True
        return OverpassResult(raw=RAW, meta={}, from_cache=False)

    monkeypatch.setattr("app.services.pipeline.OverpassClient.fetch", fake_fetch)
    settings = Settings(data_dir=tmp_path, max_area_km2=1e-9)
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    with pytest.raises(AreaTooLargeError):
        run_pipeline(
            GenerateRequest(bbox=BBOX),
            settings,
            job_dir,
            progress_cb=lambda *_: None,
            cancel_event=threading.Event(),
        )
    assert called["fetch"] is False


def test_pipeline_reports_no_buildings(tmp_path, monkeypatch):
    empty = {"elements": [{"type": "node", "id": 1, "lat": 28.2, "lon": 112.9}]}
    monkeypatch.setattr(
        "app.services.pipeline.OverpassClient.fetch",
        fake_fetch_factory(empty),
    )
    settings = Settings(data_dir=tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    with pytest.raises(NoBuildingsError):
        run_pipeline(
            GenerateRequest(bbox=BBOX),
            settings,
            job_dir,
            progress_cb=lambda *_: None,
            cancel_event=threading.Event(),
        )
