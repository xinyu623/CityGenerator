from __future__ import annotations

import math

import pytest

from app.services.heights import HeightInfo, parse_length_m, parse_levels, resolve_height


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12", 12.0),
        ("12 m", 12.0),
        ("12.5m", 12.5),
        ("12,5", 12.5),
        (" 40 ft ", 12.192),
        ("40'", 12.192),
        (18, 18.0),
        (18.5, 18.5),
    ],
)
def test_parse_length_m(raw, expected):
    assert parse_length_m(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "unknown", "abc", "0", "-3"])
def test_parse_length_m_rejects_invalid(raw):
    assert parse_length_m(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3", 3.0),
        ("3.5", 3.5),
        ("3-5", 3.0),
        ("3;4", 3.0),
        ("3 floors", 3.0),
    ],
)
def test_parse_levels(raw, expected):
    assert parse_levels(raw) == pytest.approx(expected)


def test_resolve_height_prefers_height_tag():
    info = resolve_height({"height": "24 m", "building:levels": "8"})
    assert info == HeightInfo(height_m=24.0, base_m=0.0, source="tag", clamped=False)


def test_resolve_height_uses_levels_and_roof_height():
    info = resolve_height(
        {"building:levels": "5", "roof:height": "2"},
        default_height_m=9.0,
        level_height_m=3.0,
    )
    assert info.height_m == pytest.approx(17.0)
    assert info.source == "levels"
    assert info.base_m == 0.0


def test_resolve_height_uses_default_when_missing():
    info = resolve_height({}, default_height_m=11.0)
    assert info.height_m == pytest.approx(11.0)
    assert info.source == "default"


def test_resolve_height_default_is_25_m():
    info = resolve_height({})
    assert info.height_m == pytest.approx(25.0)
    assert info.source == "default"


def test_resolve_height_uses_min_height_and_min_level():
    info = resolve_height({"height": "20", "min_height": "4"})
    assert info.base_m == 4.0

    info = resolve_height({"height": "20", "building:min_level": "2"}, level_height_m=3.5)
    assert info.base_m == 7.0


def test_resolve_height_clamps_extremes():
    info = resolve_height({"height": "999"})
    assert info.height_m == 300.0
    assert info.clamped is True

    info = resolve_height({"height": "0.5"})
    assert info.height_m == 2.0
    assert info.clamped is True


def test_resolve_height_never_below_base():
    info = resolve_height({"height": "5", "min_height": "4.5"})
    assert info.height_m > info.base_m
    assert not math.isclose(info.height_m, info.base_m)
