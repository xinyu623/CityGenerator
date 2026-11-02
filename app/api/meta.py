"""Health and configuration endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.config import Settings
from app.models.schemas import (
    ConfigResponse,
    GenerateOptions,
    HealthResponse,
    TerrainOptions,
)

router = APIRouter(tags=["meta"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    settings = _settings(request)
    return HealthResponse(status="ok", version=settings.version)


@router.get("/config", response_model=ConfigResponse)
def config(request: Request) -> ConfigResponse:
    settings = _settings(request)
    defaults = GenerateOptions(
        default_height_m=settings.default_height_m,
        level_height_m=settings.level_height_m,
        embed_depth_m=settings.embed_depth_m,
        simplify_m=settings.simplify_m,
        min_building_area_m2=settings.min_building_area_m2,
        terrain=TerrainOptions(
            enabled=settings.terrain_enabled_default,
            resolution_m=settings.terrain_resolution_m,
        ),
    )
    return ConfigResponse(
        app_name=settings.app_name,
        version=settings.version,
        max_area_km2=settings.max_area_km2,
        max_buildings=settings.max_buildings,
        defaults=defaults,
        attribution="© OpenStreetMap contributors",
        terrain_attribution="地形：AWS Terrain Tiles（Mapzen / Amazon，SRTM 派生）",
        osm_tile_url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    )
