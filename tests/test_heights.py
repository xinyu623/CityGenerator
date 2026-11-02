from __future__ import annotations

import math

import pytest

from app.services.heights import (
    HeightInfo,
    HeightSampler,
    KnownHeight,
    building_type_group,
    parse_length_m,
    parse_levels,
    resolve_height,
)


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


def test_height_sampler_draws_near_the_known_values():
    sampler = HeightSampler([12.0, 60.0, 9.0], jitter=0.1, seed=3)

    values = [sampler.draw().height_m for _ in range(200)]
    assert all(2.0 <= value <= 300.0 for value in values)
    assert min(values) >= 9.0 * 0.9 - 1e-9
    assert max(values) <= 60.0 * 1.1 + 1e-9


def test_height_sampler_is_deterministic_for_a_seed():
    first = [HeightSampler([15.0, 30.0], seed=11).draw().height_m for _ in range(5)]
    second = [HeightSampler([15.0, 30.0], seed=11).draw().height_m for _ in range(5)]
    assert first == second


def test_height_sampler_ignores_out_of_range_values():
    sampler = HeightSampler([0.5, 1.0, 40.0, 500.0], seed=0)
    assert len(sampler) == 1
    assert sampler.draw().height_m == pytest.approx(40.0, rel=0.1)


def test_height_sampler_without_values_is_not_usable():
    sampler = HeightSampler([], seed=0)
    assert not sampler.usable
    with pytest.raises(ValueError):
        sampler.draw()


def test_height_sampler_prefers_heights_from_similar_footprints():
    sampler = HeightSampler(
        [(4.0, 20.0), (12.0, 200.0), (100.0, 5000.0)],
        jitter=0.0,
        seed=5,
    )

    small = [sampler.draw(30.0).height_m for _ in range(50)]
    large = [sampler.draw(2500.0).height_m for _ in range(50)]

    # The area kernel is asymptotic, not a hard band: the near observation
    # dominates, the far ones fade out instead of being cut off.
    assert small.count(4.0) >= 40
    assert 100.0 not in small
    assert large.count(100.0) >= 40
    assert 4.0 not in large


def test_height_sampler_fades_far_observations_instead_of_cutting_them_off():
    sampler = HeightSampler([(4.0, 20.0), (100.0, 5000.0)], jitter=0.0, seed=1)

    values = {sampler.draw(1_000_000.0).height_m for _ in range(200)}

    # Neither observation is close to a 1e6 m2 target, so the kernel picks the
    # nearer one instead of falling back to a uniform pool.
    assert values == {100.0}


def test_height_sampler_samples_uniformly_when_no_observation_has_an_area():
    sampler = HeightSampler([4.0, 100.0], jitter=0.0, seed=1)

    values = {sampler.draw(1000.0).height_m for _ in range(200)}

    assert values == {4.0, 100.0}


def test_height_sampler_skips_area_less_observations_when_weighting():
    sampler = HeightSampler([(4.0, 700.0), 100.0], jitter=0.0, seed=1)

    values = {sampler.draw(700.0).height_m for _ in range(200)}

    assert values == {4.0}


def test_height_sampler_accepts_plain_heights_and_ignores_the_area():
    sampler = HeightSampler([12.0, 60.0], jitter=0.0, seed=0)

    assert sampler.draw(2500.0).height_m in {12.0, 60.0}
    assert sampler.area_weighted is False


@pytest.mark.parametrize(
    ("building_type", "group"),
    [
        ("yes", "unspecified"),
        (None, "unspecified"),
        ("", "unspecified"),
        ("apartments", "residential"),
        ("house", "residential"),
        ("retail", "commercial"),
        ("school", "civic"),
        ("train_station", "civic"),
        ("warehouse", "industrial"),
        ("tower", "other"),
    ],
)
def test_building_type_group_maps_osm_values(building_type, group):
    assert building_type_group(building_type) == group


def test_height_sampler_ignores_observations_far_from_the_target_area():
    # For a 112 m2 building the only in-band observation is the 200 m2 one; the
    # nearer-in-log-space 30 m2 shed is a different size class and must not win.
    sampler = HeightSampler(
        [KnownHeight(3.0, 30.0, "yes"), KnownHeight(12.0, 200.0, "yes")],
        jitter=0.0,
        seed=0,
    )

    values = {sampler.draw(112.0, "yes").height_m for _ in range(200)}

    assert values == {12.0}


def test_height_sampler_falls_back_to_the_nearest_observation_outside_the_band():
    sampler = HeightSampler([(4.0, 20.0), (100.0, 5000.0)], jitter=0.0, seed=1)

    values = {sampler.draw(1_000_000.0).height_m for _ in range(200)}

    # Neither observation is within 0.5-2x of a 1e6 m2 target, so the kernel
    # still picks the nearer one instead of a uniform pool.
    assert values == {100.0}


def test_area_band_edges_are_inclusive():
    sampler = HeightSampler(
        [KnownHeight(3.0, 50.0, "yes"), KnownHeight(60.0, 200.0, "yes")],
        jitter=0.0,
        seed=0,
    )

    assert {sampler.draw(100.0, "yes").height_m for _ in range(200)} == {3.0, 60.0}
    assert {sampler.draw(75.0, "yes").height_m for _ in range(200)} == {3.0}
    assert {sampler.draw(110.0, "yes").height_m for _ in range(200)} == {60.0}


def test_height_sampler_prefers_the_same_type_and_area():
    # Twenty walk-ups of 700 m2 and one 250 m tower: the kernel plus the type
    # weight must keep the tower out of a 700 m2 draw.
    known = [KnownHeight(9.0, 700.0, "yes") for _ in range(20)]
    known.append(KnownHeight(250.0, 1700.0, "yes"))
    sampler = HeightSampler(known, jitter=0.0, seed=0)

    draws = [sampler.draw(700.0, "yes") for _ in range(200)]

    assert sum(1 for draw in draws if draw.height_m == 9.0) >= 180
    assert {draw.rule for draw in draws} == {"similarity"}


def test_height_sampler_weights_the_same_type_above_another_type():
    # Same footprint area, so only the type factor can separate the two.
    known = [KnownHeight(20.0, 700.0, "apartments"), KnownHeight(60.0, 700.0, "tower")]
    sampler = HeightSampler(known, jitter=0.0, seed=1)

    heights = [sampler.draw(700.0, "apartments").height_m for _ in range(200)]

    assert heights.count(20.0) > heights.count(60.0)


def test_height_sampler_records_the_rule():
    sampler = HeightSampler([KnownHeight(200.0, 700.0, "yes")], jitter=0.0, seed=0)

    assert sampler.draw(700.0, "yes").rule == "similarity"
    assert sampler.draw(None, "yes").rule == "pool"
    capped = sampler.draw(700.0, "train_station")
    assert capped.rule == "type_cap"
    assert capped.height_m == pytest.approx(60.0)


def test_height_sampler_applies_the_conservative_slenderness_bound():
    sampler = HeightSampler(
        [KnownHeight(200.0, 400.0, "yes")],
        jitter=0.0,
        seed=0,
        conservative=True,
        slenderness_k=8.0,
    )

    draw = sampler.draw(400.0, "yes")

    assert draw.height_m == pytest.approx(160.0)
    assert draw.rule == "conservative_cap"


def test_conservative_bound_never_raises_a_height():
    sampler = HeightSampler(
        [KnownHeight(50.0, 400.0, "yes")],
        jitter=0.0,
        seed=0,
        conservative=True,
        slenderness_k=8.0,
    )

    assert sampler.draw(400.0, "yes").height_m == pytest.approx(50.0)


def test_conservative_bound_is_inactive_without_an_area():
    sampler = HeightSampler(
        [KnownHeight(200.0, 400.0, "yes")],
        jitter=0.0,
        seed=0,
        conservative=True,
    )

    assert sampler.draw(None, "yes").height_m == pytest.approx(200.0)
    assert sampler.draw(0.0, "yes").height_m == pytest.approx(200.0)


def test_type_ceiling_survives_the_conservative_bound():
    # A 100 m observation for a shed: the 10 m type ceiling wins over the
    # (looser) 80 m slenderness bound for a 100 m2 footprint.
    sampler = HeightSampler(
        [KnownHeight(100.0, 100.0, "shed")],
        jitter=0.0,
        seed=0,
        conservative=True,
        slenderness_k=8.0,
    )

    draw = sampler.draw(100.0, "shed")

    assert draw.height_m == pytest.approx(10.0)
    assert draw.rule == "type_cap"
