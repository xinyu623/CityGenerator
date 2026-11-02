from __future__ import annotations

import json
import threading
import zipfile
from pathlib import Path

import pytest

from app.config import Settings
from app.models.schemas import BBox, GenerateOptions, GenerateRequest, TerrainOptions
from app.services.errors import AreaTooLargeError, NoBuildingsError
from app.services.overpass import OverpassResult
from app.services.pipeline import run_pipeline
from app.services.stl_export import load_stl_bytes, non_manifold_edge_count

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "overpass_tiny.json"
RAW = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
BBOX = BBox(south=28.20, west=112.90, north=28.21, east=112.91)


def fake_fetch_factory(raw: dict):
    def fake_fetch(self, bbox, *, refresh=False, progress_cb=None):
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
                "overpass_splits": [
                    {
                        "bbox": {
                            "south": 28.2,
                            "west": 112.9,
                            "north": 28.205,
                            "east": 112.905,
                        },
                        "depth": 0,
                        "children": 4,
                        "error": "无法从 Overpass 获取数据",
                        "detail": "Overpass 返回 HTTP 504",
                    }
                ],
                "cell_times": [
                    {
                        "bbox": {
                            "south": 28.2,
                            "west": 112.9,
                            "north": 28.21,
                            "east": 112.91,
                        },
                        "depth": 0,
                        "seconds": 0.25,
                        "from_cache": False,
                    }
                ],
            },
            from_cache=False,
        )

    return fake_fetch


def test_pipeline_generates_expected_files(tmp_path, monkeypatch, synthetic_terrain):
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
    assert {"city.stl", "metadata.json", "README.txt"} <= names
    assert "buildings.stl" not in names
    assert "ground.stl" not in names
    assert "buildings.geojson" in names
    assert [name for name in names if name.endswith(".stl")] == ["city.stl"]

    metadata = json.loads((job_dir / "output" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["buildings"]["count"] == 3
    assert metadata["crs_projected"] == "EPSG:32649"
    assert metadata["local_frame"]["z"] == "up"
    assert metadata["ground"]["included"] is True
    assert metadata["ground"]["mode"] == "terrain"
    assert metadata["ground"]["surface"] == "test-ramp"
    assert metadata["source"]["overpass_chunks"] == 1
    assert metadata["source"]["overpass_splits"][0]["detail"] == "Overpass 返回 HTTP 504"
    assert metadata["source"]["overpass_cell_times"][0]["seconds"] == 0.25
    assert metadata["attribution"].startswith("© OpenStreetMap")
    assert metadata["buildings"]["each_shell_watertight"] is True
    assert metadata["city"]["watertight"] is True
    assert metadata["city"]["winding_consistent"] is True
    assert metadata["city"]["non_manifold_edges"] == 0
    assert metadata["city"]["union_engine"] == "manifold"
    assert metadata["city"]["triangle_count"] == result.stats["triangles"]
    assert metadata["terrain"]["enabled"] is True
    assert metadata["terrain"]["relief_m"] > 0.0
    assert metadata["terrain"]["datum_offset_m"] == pytest.approx(metadata["terrain"]["elevation_min_m"])
    assert metadata["city"]["terrain_triangle_count"] == metadata["terrain"]["triangle_count"]
    assert result.stats["terrain_enabled"] is True
    assert result.stats["terrain_relief_m"] > 0.0

    geojson = json.loads((job_dir / "output" / "buildings.geojson").read_text(encoding="utf-8"))
    assert len(geojson["features"]) == 3
    for feature in geojson["features"]:
        assert "ground_m" in feature["properties"]
        assert feature["properties"]["roof_m"] >= feature["properties"]["ground_m"] + 2.0

    city = load_stl_bytes((job_dir / "output" / "city.stl").read_bytes())
    assert city.is_watertight
    assert city.is_winding_consistent
    assert city.volume > 0.0
    assert non_manifold_edge_count(city) == 0
    assert len(city.faces) > 0
    assert city.bounds[0][2] == pytest.approx(-1.0, abs=1e-3)


def test_pipeline_without_terrain_is_flat(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.pipeline.OverpassClient.fetch",
        fake_fetch_factory(RAW),
    )
    settings = Settings(data_dir=tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    result = run_pipeline(
        GenerateRequest(
            bbox=BBOX,
            options=GenerateOptions(terrain=TerrainOptions(enabled=False)),
        ),
        settings,
        job_dir,
        progress_cb=lambda *_: None,
        cancel_event=threading.Event(),
    )

    metadata = json.loads((job_dir / "output" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["ground"]["mode"] == "slab"
    assert metadata["terrain"] == {"enabled": False}
    assert metadata["city"]["terrain_triangle_count"] == 0
    assert result.stats["terrain_enabled"] is False
    assert result.stats["terrain_relief_m"] == 0.0

    city = load_stl_bytes((job_dir / "output" / "city.stl").read_bytes())
    assert city.is_watertight
    assert non_manifold_edge_count(city) == 0


def test_pipeline_terrain_stage_is_reported(tmp_path, monkeypatch, synthetic_terrain):
    monkeypatch.setattr(
        "app.services.pipeline.OverpassClient.fetch",
        fake_fetch_factory(RAW),
    )
    settings = Settings(data_dir=tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    stages: list[str] = []

    run_pipeline(
        GenerateRequest(bbox=BBOX),
        settings,
        job_dir,
        progress_cb=lambda percent, stage, message: stages.append(stage),
        cancel_event=threading.Event(),
    )

    assert "terrain" in stages
    assert stages[-1] == "completed"


def test_pipeline_rejects_oversized_area(tmp_path, monkeypatch):
    called = {"fetch": False}

    def fake_fetch(self, bbox, *, refresh=False, progress_cb=None):
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
