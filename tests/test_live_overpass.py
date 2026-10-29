from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.models.schemas import BBox
from app.services.overpass import OverpassClient

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("CITYGEN_RUN_NETWORK") != "1",
        reason="set CITYGEN_RUN_NETWORK=1 to run live Overpass tests",
    ),
]


def test_live_overpass_returns_buildings(tmp_path):
    settings = Settings(data_dir=tmp_path)
    client = OverpassClient(settings)
    try:
        result = client.fetch(
            BBox(south=28.1900, west=112.9700, north=28.1920, east=112.9720),
            refresh=True,
        )
    finally:
        client.close()

    assert result.raw.get("elements")
    assert result.meta["query_version"] == "buildings-v1"
    assert result.meta["query_hash"]
