"""Application configuration.

All paths and tunable limits live here so the pipeline and the API share one
source of truth. Values can be overridden with ``CITYGEN_*`` environment
variables or a local ``.env`` file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CITYGEN_",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "CityGenerator"
    version: str = "0.1.0"

    data_dir: Path = DEFAULT_DATA_DIR
    max_workers: int = 2

    # Request guards.
    max_area_km2: float = 4.0
    max_buildings: int = 20_000

    # Overpass.
    overpass_endpoints: str = (
        "https://overpass-api.de/api/interpreter,"
        "https://overpass.kumi.systems/api/interpreter,"
        "https://overpass.private.coffee/api/interpreter"
    )
    overpass_timeout_s: int = 180
    overpass_min_interval_s: float = 1.0
    overpass_max_retries: int = 3
    overpass_user_agent: str = "CityGenerator/0.1 (local research tool)"

    # Retention.
    job_ttl_days: int = 7
    cache_ttl_days: int = 7

    # Defaults echoed by /api/config and used by the UI.
    default_height_m: float = 25.0
    level_height_m: float = 3.0
    embed_depth_m: float = 0.5
    simplify_m: float = 0.05
    min_building_area_m2: float = 4.0
    ground_slab_thickness_m: float = 1.0

    @property
    def endpoints(self) -> list[str]:
        return [item.strip() for item in self.overpass_endpoints.split(",") if item.strip()]

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    def ensure_dirs(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
