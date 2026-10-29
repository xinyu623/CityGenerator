"""Projection helpers: WGS84 -> UTM -> a local metric frame."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from pyproj import Transformer


def utm_zone_for(longitude: float, latitude: float) -> int:
    zone = int(math.floor((longitude + 180.0) / 6.0)) + 1
    return min(max(zone, 1), 60)


def utm_epsg_for(longitude: float, latitude: float) -> int:
    zone = utm_zone_for(longitude, latitude)
    return (32600 if latitude >= 0 else 32700) + zone


@dataclass
class Projector:
    """A UTM transformer plus a local origin.

    ``project`` returns absolute UTM coordinates. ``to_local`` subtracts the
    origin so the values written to STL stay small enough for float32.
    """

    transformer: Transformer
    epsg: int
    zone: int
    origin_easting: float = 0.0
    origin_northing: float = 0.0
    _inverse: Transformer = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._inverse = Transformer.from_crs(f"EPSG:{self.epsg}", "EPSG:4326", always_xy=True)

    @property
    def crs(self) -> str:
        return f"EPSG:{self.epsg}"

    def project(self, longitude: float, latitude: float) -> tuple[float, float]:
        x, y = self.transformer.transform(longitude, latitude)
        return float(x), float(y)

    def set_origin(self, easting: float, northing: float) -> None:
        self.origin_easting = float(easting)
        self.origin_northing = float(northing)

    def to_local(self, easting: float, northing: float) -> tuple[float, float]:
        return easting - self.origin_easting, northing - self.origin_northing

    def local_to_wgs84(self, x: float, y: float) -> tuple[float, float]:
        easting = x + self.origin_easting
        northing = y + self.origin_northing
        longitude, latitude = self._inverse.transform(easting, northing)
        return float(longitude), float(latitude)

    def origin_wgs84(self) -> tuple[float, float]:
        return self.local_to_wgs84(0.0, 0.0)


def projector_for_bbox(south: float, west: float, north: float, east: float) -> Projector:
    """Create a projector whose origin is the south-west corner of the bbox."""

    center_lon = (west + east) / 2.0
    center_lat = (south + north) / 2.0
    epsg = utm_epsg_for(center_lon, center_lat)
    zone = utm_zone_for(center_lon, center_lat)
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)

    corners = [
        transformer.transform(west, south),
        transformer.transform(east, south),
        transformer.transform(west, north),
        transformer.transform(east, north),
    ]
    origin_easting = min(float(x) for x, _ in corners)
    origin_northing = min(float(y) for _, y in corners)
    return Projector(
        transformer=transformer,
        epsg=epsg,
        zone=zone,
        origin_easting=origin_easting,
        origin_northing=origin_northing,
    )
