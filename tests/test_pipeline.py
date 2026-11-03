from __future__ import annotations

import json
import threading
import zipfile
from pathlib import Path

import pytest
import trimesh
from shapely.geometry import Polygon

from app.config import Settings
from app.models.schemas import BBox, GenerateOptions, GenerateRequest, TerrainOptions
from app.services.buildings import BuildingRecord, BuildResult
from app.services.city import FusedCity, fuse_city
from app.services.errors import AreaTooLargeError, CityUnionError, NoBuildingsError
from app.services.ground import build_ground
from app.services.output import _geojson, write_outputs
from app.services.overpass import OverpassResult
from app.services.pipeline import run_pipeline
from app.services.projection import projector_for_bbox
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
    assert {"city.stl", "metadata.json", "README.txt", "patches.json"} <= names
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
    timings = result.stats["timings_s"]
    assert {"fetch", "build", "ground", "fuse", "export", "total"} <= set(timings)
    assert timings["total"] >= timings["fuse"] >= 0.0
    assert metadata["timings_s"] == timings
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

    patches = json.loads((job_dir / "output" / "patches.json").read_text(encoding="utf-8"))
    assert "patches.json" in metadata["files"]
    assert patches["triangle_count"] == len(city.faces)
    assert patches["ground_normal_dot_min"] == pytest.approx(0.9)
    assert metadata["patches"]["ground_normal_dot_min"] == pytest.approx(0.9)
    assert result.stats["patch_counts"] == patches["counts"]
    assert result.stats["patch_ranges"] == metadata["patches"]["range_count"]
    assert sum(patches["counts"].values()) == len(city.faces)
    assert metadata["patches"]["counts"] == patches["counts"]
    assert patches["counts"]["ground"] > 0
    assert patches["counts"]["wall"] > 0
    assert patches["counts"]["bottom"] > 0
    normals = city.face_normals
    covered = 0
    for item in patches["ranges"]:
        assert item["start"] == covered
        covered += item["count"]
        block = normals[item["start"] : item["start"] + item["count"]]
        if item["kind"] == "ground":
            assert (block[:, 2] > 0.0).all()
        elif item["kind"] == "bottom":
            assert (block[:, 2] < 0.0).all()
    assert covered == len(city.faces)


def test_pipeline_writes_the_fused_stl_without_reexporting_it(
    tmp_path, monkeypatch, synthetic_terrain
):
    # The export stage must write the bytes fuse_city already round-tripped and
    # validated: one export remains (the preview mesh), not two.
    from app.services import output as output_module

    exported: list[int] = []
    original = output_module.export_stl_bytes

    def counting_export(mesh):
        exported.append(len(mesh.faces))
        return original(mesh)

    monkeypatch.setattr(output_module, "export_stl_bytes", counting_export)
    monkeypatch.setattr(
        "app.services.pipeline.OverpassClient.fetch",
        fake_fetch_factory(RAW),
    )
    settings = Settings(data_dir=tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    result = run_pipeline(
        GenerateRequest(bbox=BBOX),
        settings,
        job_dir,
        progress_cb=lambda *args: None,
        cancel_event=threading.Event(),
    )

    metadata = json.loads((job_dir / "output" / "metadata.json").read_text(encoding="utf-8"))
    city_bytes = (job_dir / "output" / "city.stl").read_bytes()
    assert len(exported) == 1
    assert len(city_bytes) == 84 + 50 * metadata["city"]["triangle_count"]
    assert metadata["city"]["non_manifold_edges"] == 0
    assert result.stats["triangles"] == metadata["city"]["triangle_count"]


def test_pipeline_skips_the_engine_input_volume_check(tmp_path, monkeypatch, synthetic_terrain):
    # extrude_prepared validates every shell, so the pipeline tells the boolean
    # engine not to repeat that per-shell census (it was the slowest part of
    # the fuse stage after the build got cheaper).
    from app.services import pipeline as pipeline_module

    seen: dict[str, object] = {}
    original = pipeline_module.fuse_city

    def spy(buildings, base, **kwargs):
        seen.update(kwargs)
        return original(buildings, base, **kwargs)

    monkeypatch.setattr(pipeline_module, "fuse_city", spy)
    monkeypatch.setattr(
        "app.services.pipeline.OverpassClient.fetch",
        fake_fetch_factory(RAW),
    )
    settings = Settings(data_dir=tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    result = run_pipeline(
        GenerateRequest(bbox=BBOX),
        settings,
        job_dir,
        progress_cb=lambda *args: None,
        cancel_event=threading.Event(),
    )

    assert seen["check_input_volumes"] is False
    assert result.stats["city_watertight"] is True


def _hand_built_output(
    mesh: trimesh.Trimesh | None,
) -> tuple[FusedCity, BuildResult, GenerateRequest]:
    """A FusedCity without validated bytes, as a direct caller could build it."""

    footprint = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)])
    shell = trimesh.creation.box(extents=(10.0, 10.0, 12.0))
    shell.apply_translation((5.0, 5.0, 5.5))
    slab = build_ground((0.0, 0.0, 20.0, 20.0), mode="slab", thickness_m=1.0)
    fused = fuse_city([shell], slab)
    city = FusedCity(
        mesh=fused.mesh if mesh is None else mesh,
        base_bounds=fused.base_bounds,
        base_thickness_m=1.0,
        base_mesh=fused.base_mesh,
    )
    record = BuildingRecord(
        osm_type="way",
        osm_id=1,
        tags={"building": "yes"},
        source_geometry=footprint,
        polygon_local=footprint,
        height_m=12.0,
        base_m=0.0,
        height_source="tag",
        area_m2=100.0,
        mesh=shell,
    )
    build_result = BuildResult(
        meshes=[shell],
        records=[record],
        bounds=(0.0, 0.0, 10.0, 10.0),
    )
    return city, build_result, GenerateRequest(bbox=BBOX)


def test_write_outputs_exports_a_hand_built_fused_city(tmp_path):
    # The defensive path for a FusedCity without validated bytes must still
    # export, validate, and write a complete bundle.
    city, build_result, request = _hand_built_output(None)
    assert city.stl_bytes is None

    bundle = write_outputs(
        job_id="job",
        job_dir=tmp_path,
        request=request,
        projector=projector_for_bbox(*BBOX.as_tuple()),
        build_result=build_result,
        overpass_meta={},
        area_km2=0.0004,
        city=city,
        settings=Settings(data_dir=tmp_path),
        warnings=[],
    )

    city_path = tmp_path / "output" / "city.stl"
    assert city_path.is_file()
    loaded = load_stl_bytes(city_path.read_bytes())
    assert loaded.is_watertight
    assert non_manifold_edge_count(loaded) == 0
    assert bundle.triangle_count == len(city.mesh.faces)
    metadata = json.loads((tmp_path / "output" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["city"]["triangle_count"] == len(city.mesh.faces)


def test_write_outputs_does_not_write_an_unvalidated_fallback_city(tmp_path):
    closed = trimesh.creation.box(extents=(10.0, 10.0, 12.0))
    open_mesh = trimesh.Trimesh(
        vertices=closed.vertices,
        faces=closed.faces[:-1],
        process=False,
    )
    city, build_result, request = _hand_built_output(open_mesh)

    with pytest.raises(CityUnionError):
        write_outputs(
            job_id="job",
            job_dir=tmp_path,
            request=request,
            projector=projector_for_bbox(*BBOX.as_tuple()),
            build_result=build_result,
            overpass_meta={},
            area_km2=0.0004,
            city=city,
            settings=Settings(data_dir=tmp_path),
            warnings=[],
        )

    assert not (tmp_path / "output" / "city.stl").exists()
    assert not (tmp_path / "output" / "preview.stl").exists()


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

    patches = json.loads((job_dir / "output" / "patches.json").read_text(encoding="utf-8"))
    assert patches["counts"]["ground"] > 0  # the flat slab top at z=0
    assert patches["counts"]["bottom"] > 0


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


def test_geojson_marks_the_sampling_rule_only_for_sampled_buildings():
    polygon = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    sampled = BuildingRecord(
        osm_type="way",
        osm_id=1,
        tags={},
        source_geometry=polygon,
        polygon_local=polygon,
        height_m=12.0,
        base_m=0.0,
        height_source="sampled",
        area_m2=100.0,
        mesh=None,  # type: ignore[arg-type]
        height_rule="similarity",
    )
    tagged = BuildingRecord(
        osm_type="way",
        osm_id=2,
        tags={},
        source_geometry=polygon,
        polygon_local=polygon,
        height_m=20.0,
        base_m=0.0,
        height_source="tag",
        area_m2=100.0,
        mesh=None,  # type: ignore[arg-type]
    )

    features = _geojson(BuildResult(records=[sampled, tagged]))["features"]

    assert features[0]["properties"]["height_rule"] == "similarity"
    assert "height_rule" not in features[1]["properties"]


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


def test_origin_zero_puts_the_solid_corner_at_the_origin(tmp_path, monkeypatch):
    # Move one footprint outside the bbox so the raw frame starts negative,
    # which is exactly the case the flag exists for.
    raw = json.loads(json.dumps(RAW))
    for element in raw["elements"]:
        if element.get("type") == "node" and element.get("id") in (1, 2, 3, 4):
            element["lon"] -= 0.0004
            element["lat"] -= 0.0003
    monkeypatch.setattr("app.services.pipeline.OverpassClient.fetch", fake_fetch_factory(raw))

    options = GenerateOptions(terrain=TerrainOptions(enabled=False))

    def build(name: str, **settings_kwargs):
        job = tmp_path / name
        job.mkdir()
        run_pipeline(
            GenerateRequest(bbox=BBOX, options=options),
            Settings(data_dir=tmp_path, max_area_km2=4.0, **settings_kwargs),
            job,
        )
        return job

    def corner(job: Path) -> tuple[float, float]:
        mesh = load_stl_bytes((job / "output" / "city.stl").read_bytes())
        vertices = mesh.vertices
        return float(vertices[:, 0].min()), float(vertices[:, 1].min())

    raw_job = build("raw")
    shifted_job = build("shifted", origin_at_min_xy=True)

    raw_x, raw_y = corner(raw_job)
    assert raw_x < -20.0 and raw_y < -20.0

    shifted_x, shifted_y = corner(shifted_job)
    assert shifted_x == pytest.approx(0.0, abs=1e-3)
    assert shifted_y == pytest.approx(0.0, abs=1e-3)

    metadata = json.loads((shifted_job / "output" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["local_frame"]["origin_mode"] == "min_xy"
    assert metadata["ground"]["bounds_local"][0] == pytest.approx(0.0, abs=1e-3)
    assert metadata["ground"]["bounds_local"][1] == pytest.approx(0.0, abs=1e-3)
    # Translation must not change size or triangle count.
    assert (shifted_job / "output" / "city.stl").stat().st_size == (
        raw_job / "output" / "city.stl"
    ).stat().st_size
