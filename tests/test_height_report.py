from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.height_report import check, format_report, load_output, main, summarize


def _feature(
    osm_id: int,
    height_m: float,
    *,
    area_m2: float = 1000.0,
    building: str = "yes",
    source: str = "sampled",
    rule: str = "similarity",
) -> dict:
    return {
        "type": "Feature",
        "geometry": None,
        "properties": {
            "osm_id": osm_id,
            "height_m": height_m,
            "height_source": source,
            "height_rule": rule,
            "area_m2": area_m2,
            "tags": {"building": building},
        },
    }


def _metadata(
    *,
    known: int = 10,
    pool_above_100: int = 0,
    pool_above_250: int = 0,
    conservative: bool = False,
    slenderness_k: float = 8.0,
) -> dict:
    return {
        "buildings": {
            "height_sampling": {
                "enabled": True,
                "known": known,
                "sampled": 10,
                "jitter": 0.1,
                "seed": 0,
                "weighting": "type_and_area_similarity",
                "conservative": conservative,
                "slenderness_k": slenderness_k,
                "rules": {"similarity": 10},
                "type_capped": 0,
                "pool_above_100m": pool_above_100,
                "pool_above_250m": pool_above_250,
                "sampled_above_100m": 0,
                "sampled_above_250m": 0,
                "above_100m_by_group": {},
            }
        }
    }


def test_height_report_flags_a_sampled_tail_above_the_pool_share():
    features = [
        _feature(1, 300.0),
        _feature(2, 280.0),
        _feature(3, 260.0),
        *[_feature(index, 9.0) for index in range(4, 11)],
    ]

    summary = summarize(_metadata(), features)

    assert summary["sampled"]["count"] == 10
    assert summary["sampled"]["above_250m"] == 3
    assert summary["sampled"]["above_250m_share"] == pytest.approx(0.3)
    assert any("250" in line for line in check(summary))


def test_height_report_passes_when_the_tail_matches_the_pool():
    features = [_feature(1, 150.0), *[_feature(index, 9.0) for index in range(2, 11)]]

    summary = summarize(_metadata(pool_above_100=2), features)

    assert summary["sampled"]["above_100m_share"] == pytest.approx(0.1)
    assert check(summary) == []


def test_height_report_flags_a_height_above_its_type_ceiling():
    features = [_feature(1, 120.0, building="school")]

    summary = summarize(_metadata(pool_above_100=10), features)

    assert any("school" in line for line in check(summary))


def test_height_report_flags_a_conservative_bound_violation():
    features = [_feature(1, 200.0, area_m2=400.0)]

    summary = summarize(_metadata(conservative=True, slenderness_k=8.0), features)

    assert summary["sampled"]["above_250m"] == 0
    violations = check(summary)
    assert any("slenderness" in line for line in violations)


def test_height_report_aggregates_multipart_buildings():
    # One building drawn once from its total footprint area (2 x 100 m2 parts)
    # must not look like a 50 m tower on a 100 m2 plot.
    features = [_feature(7, 50.0, area_m2=100.0), _feature(7, 50.0, area_m2=100.0)]

    summary = summarize(_metadata(), features)

    assert summary["sampled"]["count"] == 1
    assert summary["slenderness"][0]["area_m2"] == pytest.approx(200.0)
    assert summary["slenderness"][0]["ratio"] == pytest.approx(50.0 / 200.0**0.5)


def test_height_report_loads_a_job_directory(tmp_path: Path):
    output = tmp_path / "output"
    output.mkdir(parents=True)
    (output / "metadata.json").write_text(json.dumps(_metadata()), encoding="utf-8")
    (output / "buildings.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [_feature(1, 12.0)]}),
        encoding="utf-8",
    )

    metadata, features = load_output(tmp_path)

    assert metadata["buildings"]["height_sampling"]["known"] == 10
    assert len(features) == 1


def test_height_report_main_exits_non_zero_on_a_failed_check(tmp_path: Path, capsys):
    output = tmp_path / "output"
    output.mkdir(parents=True)
    (output / "metadata.json").write_text(json.dumps(_metadata()), encoding="utf-8")
    (output / "buildings.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [_feature(1, 300.0), *[_feature(index, 9.0) for index in range(2, 11)]],
            }
        ),
        encoding="utf-8",
    )

    code = main(["--output", str(tmp_path), "--check"])

    assert code == 1
    assert "FAILED" in capsys.readouterr().out


def test_height_report_format_mentions_the_rules_and_the_pool():
    features = [_feature(1, 12.0, rule="type_cap")]

    text = format_report(summarize(_metadata(), features))

    assert "sampled" in text.lower()
    assert "type_cap" in text
