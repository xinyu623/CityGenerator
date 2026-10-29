from __future__ import annotations

import pytest

from app.services.projection import projector_for_bbox


def test_utm_zone_for_changsha():
    projector = projector_for_bbox(south=28.20, west=112.90, north=28.21, east=112.91)
    assert projector.epsg == 32649
    assert projector.zone == 49


def test_project_and_round_trip():
    projector = projector_for_bbox(south=28.20, west=112.90, north=28.21, east=112.91)
    x, y = projector.project(112.905, 28.205)
    assert x > 0
    assert y > 0

    local_x, local_y = projector.to_local(x, y)
    lon, lat = projector.local_to_wgs84(local_x, local_y)
    assert lon == pytest.approx(112.905, abs=1e-9)
    assert lat == pytest.approx(28.205, abs=1e-9)


def test_local_origin_is_non_negative():
    projector = projector_for_bbox(south=28.20, west=112.90, north=28.21, east=112.91)
    for lon, lat in [
        (112.90, 28.20),
        (112.91, 28.20),
        (112.90, 28.21),
        (112.91, 28.21),
    ]:
        x, y = projector.project(lon, lat)
        assert x >= -1e-6
        assert y >= -1e-6


def test_set_origin_moves_local_frame():
    projector = projector_for_bbox(south=28.20, west=112.90, north=28.21, east=112.91)
    x0, y0 = projector.project(112.905, 28.205)
    projector.set_origin(x0, y0)
    assert projector.to_local(x0, y0) == pytest.approx((0.0, 0.0))
