"""Domain exceptions with stable API error codes."""

from __future__ import annotations


class CityGeneratorError(Exception):
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class OverpassError(CityGeneratorError):
    code = "OVERPASS_ERROR"


class NoBuildingsError(CityGeneratorError):
    code = "NO_BUILDINGS"


class OsmParseError(CityGeneratorError):
    code = "OSM_PARSE_ERROR"


class GeometryError(CityGeneratorError):
    code = "GEOMETRY_ERROR"


class CityUnionError(CityGeneratorError):
    code = "CITY_UNION_FAILED"


class TerrainError(CityGeneratorError):
    code = "TERRAIN_FETCH_FAILED"


class TerrainDataError(CityGeneratorError):
    code = "TERRAIN_DATA_MISSING"


class JobCancelled(CityGeneratorError):
    code = "JOB_CANCELLED"


class JobBusyError(CityGeneratorError):
    code = "JOB_BUSY"


class JobNotReadyError(CityGeneratorError):
    code = "JOB_NOT_READY"


class BoundsError(CityGeneratorError):
    code = "BBOX_INVALID"


class AreaTooLargeError(CityGeneratorError):
    code = "AREA_TOO_LARGE"


class TooManyBuildingsError(CityGeneratorError):
    code = "TOO_MANY_BUILDINGS"
