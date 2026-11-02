"""Pydantic request and response models shared by the API and pipeline."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class BBox(BaseModel):
    south: float = Field(ge=-80.0, le=84.0)
    west: float = Field(ge=-180.0, le=180.0)
    north: float = Field(ge=-80.0, le=84.0)
    east: float = Field(ge=-180.0, le=180.0)

    @model_validator(mode="after")
    def _check_order(self) -> BBox:
        if self.north <= self.south:
            raise ValueError("north must be greater than south")
        if self.east <= self.west:
            raise ValueError("east must be greater than west (antimeridian is unsupported)")
        return self

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.south, self.west, self.north, self.east)

    def as_overpass(self) -> str:
        return f"{self.south:.6f},{self.west:.6f},{self.north:.6f},{self.east:.6f}"


class TerrainOptions(BaseModel):
    """Terrain relief (DEM) generation options."""

    enabled: bool = True
    resolution_m: float = Field(default=30.0, ge=5.0, le=50.0)


class GenerateOptions(BaseModel):
    default_height_m: float = Field(default=25.0, ge=2.0, le=300.0)
    level_height_m: float = Field(default=3.0, ge=2.0, le=10.0)
    embed_depth_m: float = Field(default=0.5, ge=0.0, le=5.0)
    ground_margin_m: float = Field(default=0.0, ge=0.0, le=500.0)
    simplify_m: float = Field(default=0.05, ge=0.0, le=2.0)
    min_building_area_m2: float = Field(default=4.0, ge=1.0, le=1000.0)
    terrain: TerrainOptions = Field(default_factory=TerrainOptions)


class GenerateRequest(BaseModel):
    bbox: BBox
    options: GenerateOptions = Field(default_factory=GenerateOptions)
    refresh: bool = False


class WarningItem(BaseModel):
    code: str
    message: str
    count: int = 1


class ErrorInfo(BaseModel):
    code: str
    message: str
    detail: str | None = None


class FileInfo(BaseModel):
    name: str
    size: int
    url: str


class JobStatus(BaseModel):
    id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    progress: int = 0
    stage: str = "queued"
    message: str = ""
    created_at: str
    updated_at: str
    stats: dict[str, Any] = Field(default_factory=dict)
    warnings: list[WarningItem] = Field(default_factory=list)
    error: ErrorInfo | None = None
    files: list[FileInfo] = Field(default_factory=list)
    download_url: str | None = None
    request: GenerateRequest | None = None


class JobCreateResponse(BaseModel):
    job_id: str
    status: str


class ConfigResponse(BaseModel):
    app_name: str
    version: str
    max_area_km2: float
    max_buildings: int
    defaults: GenerateOptions
    attribution: str
    terrain_attribution: str = ""
    osm_tile_url: str


class HealthResponse(BaseModel):
    status: str
    version: str
