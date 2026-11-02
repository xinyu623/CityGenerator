"""Application configuration.

All paths and tunable limits live here so the pipeline and the API share one
source of truth. Values can be overridden with ``CITYGEN_*`` environment
variables or a local ``.env`` file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
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
    version: str = "2.3.0"

    data_dir: Path = DEFAULT_DATA_DIR
    max_workers: int = 2

    # Request guards. A selection may span several Overpass chunks; each chunk
    # is at most `overpass_chunk_area_km2`, fetched and cached separately, then
    # merged, so a busy public Overpass never sees one huge query.
    # 17 km² keeps an exact 4 x 4 km selection (16.005 km² on the ellipsoid)
    # comfortably inside the cap.
    max_area_km2: float = 17.0
    max_buildings: int = 20_000
    overpass_chunk_area_km2: float = 4.0

    # Overpass.
    overpass_endpoints: str = (
        "https://overpass-api.de/api/interpreter,"
        "https://overpass.kumi.systems/api/interpreter,"
        "https://overpass.private.coffee/api/interpreter"
    )
    overpass_timeout_s: int = 180
    overpass_min_interval_s: float = 1.0
    overpass_max_retries: int = 3
    # A cell that exhausts its retries is retried as four smaller cells instead
    # of failing the job. Two levels keep the worst case bounded (16 children)
    # and 0.25 km² keeps a split request large enough to be worth sending.
    overpass_split_max_depth: int = 2
    overpass_split_min_area_km2: float = 0.25
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
    # patches.json marks an upward triangle as ground when its centroid and all
    # three vertices are this close to the terrain surface and its normal agrees
    # with the terrain normal (dot >= the second value); everything upward and
    # higher is a roof.
    patch_ground_tolerance_m: float = 0.05
    patch_ground_normal_dot_min: float = 0.9
    # Touching footprints are grown by half of this before extrusion so their
    # volumes genuinely overlap. That keeps the union manifold without the old
    # 0.4 m weld boxes; 0 disables it.
    contact_overlap_m: float = 0.1
    # Untagged buildings draw from the selection's own OSM height distribution
    # instead of all sharing one global default.
    height_sampling_enabled: bool = True
    height_sampling_min_samples: int = 5
    height_sampling_jitter: float = 0.1
    height_sampling_seed: int = 0
    # Optional calmer skyline: cap a sampled height at k * sqrt(footprint area).
    # Off by default; k = 8 keeps ordinary buildings and trims only the
    # implausibly slender tail.
    height_sampling_conservative: bool = False
    height_sampling_slenderness_k: float = Field(default=8.0, allow_inf_nan=False)
    # Ceiling for a tagged height. Raise it for a selection holding a supertall
    # (Lujiazui 632 m, Beijing CBD 528 m) so the landmark keeps its recorded
    # height; sampled heights keep their own 300 m cap.
    max_height_m: float = 300.0

    # Terrain / DEM elevation source.
    terrain_enabled_default: bool = True
    terrain_resolution_m: float = 30.0
    max_terrain_cells: int = 80_000
    dem_url_template: str = (
        "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
    )
    dem_zoom_min: int = 10
    dem_zoom_max: int = 14
    dem_max_tiles: int = 24
    dem_timeout_s: float = 20.0
    dem_max_retries: int = 3
    dem_user_agent: str = "CityGenerator/2.0 (local research tool)"

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
