from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.errors import JobCancelled

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "overpass_tiny.json"
RAW = __import__("json").loads(FIXTURE_PATH.read_text(encoding="utf-8"))
BBOX = {"south": 28.20, "west": 112.90, "north": 28.21, "east": 112.91}


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def test_health_and_config(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    config = client.get("/api/config")
    assert config.status_code == 200
    payload = config.json()
    assert payload["max_area_km2"] > 0
    assert payload["attribution"].startswith("© OpenStreetMap")
    assert payload["defaults"]["default_height_m"] == 25.0
    assert payload["defaults"]["terrain"]["enabled"] is True
    assert payload["defaults"]["terrain"]["resolution_m"] > 0
    assert "Terrain" in payload["terrain_attribution"] or "地形" in payload["terrain_attribution"]


def test_invalid_terrain_options_are_rejected(client):
    response = client.post(
        "/api/jobs",
        json={
            "bbox": BBOX,
            "options": {"terrain": {"enabled": True, "resolution_m": 0.5}},
        },
    )
    assert response.status_code == 422


def test_invalid_bbox_is_rejected(client):
    response = client.post(
        "/api/jobs",
        json={"bbox": {"south": 28.21, "west": 112.90, "north": 28.20, "east": 112.91}},
    )
    assert response.status_code == 422


def test_missing_job_returns_404(client):
    assert client.get("/api/jobs/does-not-exist").status_code == 404


def test_job_lifecycle_and_download(client, monkeypatch, synthetic_terrain):
    assert synthetic_terrain is not None
    from app.services.overpass import OverpassResult

    def fake_fetch(self, bbox, *, refresh=False, progress_cb=None):
        return OverpassResult(
            raw=RAW,
            meta={
                "query_version": "test",
                "query_hash": "test-hash",
                "endpoint": "https://overpass.example/api",
                "fetched_at": "2026-10-29T00:00:00+00:00",
                "osm_base_timestamp": "2026-10-29T00:00:00Z",
            },
            from_cache=False,
        )

    monkeypatch.setattr("app.services.pipeline.OverpassClient.fetch", fake_fetch)

    response = client.post("/api/jobs", json={"bbox": BBOX})
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    job = None
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.02)

    assert job is not None
    assert job["status"] == "completed", job
    assert job["stats"]["buildings"] == 3
    assert job["download_url"].endswith("/download")

    download = client.get(job["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(__import__("io").BytesIO(download.content)) as archive:
        names = archive.namelist()
        assert "city.stl" in names
        assert "buildings.stl" not in names
        assert "ground.stl" not in names

    stl_file = next(item for item in job["files"] if item["name"] == "city.stl")
    stl_response = client.get(stl_file["url"])
    assert stl_response.status_code == 200
    assert stl_response.headers["content-type"].startswith("model/stl")
    assert len(stl_response.content) > 84

    preview_response = client.get(f"/api/jobs/{job_id}/preview.stl")
    assert preview_response.status_code == 200
    assert preview_response.headers["content-type"].startswith("model/stl")
    assert len(preview_response.content) > 84

    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_cancel_running_job(client, monkeypatch):
    def slow_pipeline(request, settings, job_dir, *, progress_cb, cancel_event):
        progress_cb(10, "building", "slow")
        for _ in range(200):
            if cancel_event.is_set():
                raise JobCancelled("cancelled")
            time.sleep(0.01)
        raise AssertionError("cancel was not observed")

    monkeypatch.setattr("app.services.jobs.run_pipeline", slow_pipeline)
    response = client.post("/api/jobs", json={"bbox": BBOX})
    job_id = response.json()["job_id"]

    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "running":
            break
        time.sleep(0.01)

    deleted = client.delete(f"/api/jobs/{job_id}")
    assert deleted.status_code in {200, 409}
    if deleted.status_code == 200:
        assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_area_too_large_is_a_job_error(tmp_path, monkeypatch):
    from app.services.overpass import OverpassResult

    def fake_fetch(self, bbox, *, refresh=False, progress_cb=None):
        return OverpassResult(raw=RAW, meta={}, from_cache=False)

    monkeypatch.setattr("app.services.pipeline.OverpassClient.fetch", fake_fetch)
    app = create_app(Settings(data_dir=tmp_path, max_area_km2=1e-9))
    with TestClient(app) as client:
        job_id = client.post("/api/jobs", json={"bbox": BBOX}).json()["job_id"]
        job = None
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"failed", "completed"}:
                break
            time.sleep(0.02)
        assert job["status"] == "failed"
        assert job["error"]["code"] == "AREA_TOO_LARGE"
