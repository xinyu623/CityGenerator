"""Parse OSM height tags into metric building heights."""

from __future__ import annotations

import math
import random
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_FEET_MARKERS = ("ft", "feet", "foot", "'")

# Sampling weights. A draw prefers observations of the same building type, then
# of the same type group, and observations whose footprint area is close to the
# target's on a log scale. The area kernel is asymptotic rather than a hard band:
# a far-away observation fades out instead of being cut off, so a pool with no
# close match still samples instead of failing over to the whole pool.
TYPE_WEIGHT = 2.0
GROUP_WEIGHT = 1.5
AREA_BANDWIDTH_LOG = 0.7

# ``building`` tag values, grouped so a sparse tag can borrow from its neighbours
# instead of falling back to the whole pool.
_TYPE_GROUPS: dict[str, str] = {
    "house": "residential",
    "detached": "residential",
    "residential": "residential",
    "apartments": "residential",
    "dormitory": "residential",
    "terrace": "residential",
    "semidetached_house": "residential",
    "bungalow": "residential",
    "cabin": "residential",
    "hut": "residential",
    "farm": "residential",
    "barn": "residential",
    "houseboat": "residential",
    "commercial": "commercial",
    "retail": "commercial",
    "office": "commercial",
    "hotel": "commercial",
    "supermarket": "commercial",
    "mall": "commercial",
    "kiosk": "commercial",
    "marketplace": "commercial",
    "shop": "commercial",
    "restaurant": "commercial",
    "bank": "commercial",
    "cinema": "commercial",
    "school": "civic",
    "university": "civic",
    "college": "civic",
    "kindergarten": "civic",
    "hospital": "civic",
    "clinic": "civic",
    "museum": "civic",
    "theatre": "civic",
    "stadium": "civic",
    "sports_hall": "civic",
    "government": "civic",
    "public": "civic",
    "civic": "civic",
    "train_station": "civic",
    "transportation": "civic",
    "station": "civic",
    "subway_entrance": "civic",
    "church": "civic",
    "temple": "civic",
    "mosque": "civic",
    "community_centre": "civic",
    "library": "civic",
    "industrial": "industrial",
    "factory": "industrial",
    "warehouse": "industrial",
    "hangar": "industrial",
    "garage": "industrial",
    "garages": "industrial",
    "parking": "industrial",
    "shed": "industrial",
    "service": "industrial",
    "roof": "industrial",
    "construction": "industrial",
    "manufacture": "industrial",
}

_UNSPECIFIED_TYPES = frozenset({"", "yes", "true", "building"})


def building_type_group(building_type: str | None) -> str:
    """Group an OSM ``building`` value for height-similarity weighting."""

    key = (building_type or "").strip().lower()
    if key in _UNSPECIFIED_TYPES:
        return "unspecified"
    return _TYPE_GROUPS.get(key, "other")

# OSM tags are sparse on low-rise civic and industrial buildings, so a draw from
# the tagged pool can hand a station or a school the height of a nearby tower.
# These ceilings keep sampled heights plausible for types where a skyscraper
# makes no sense; tagged heights are still trusted as-is.
SAMPLED_HEIGHT_CAPS_M: dict[str, float] = {
    "train_station": 60.0,
    "transportation": 30.0,
    "station": 60.0,
    "subway_entrance": 8.0,
    "school": 35.0,
    "university": 60.0,
    "college": 60.0,
    "kindergarten": 12.0,
    "hospital": 60.0,
    "clinic": 25.0,
    "museum": 45.0,
    "theatre": 40.0,
    "cinema": 25.0,
    "stadium": 60.0,
    "sports_hall": 45.0,
    "government": 60.0,
    "public": 45.0,
    "civic": 45.0,
    "retail": 30.0,
    "supermarket": 25.0,
    "mall": 60.0,
    "marketplace": 20.0,
    "industrial": 25.0,
    "factory": 25.0,
    "warehouse": 25.0,
    "hangar": 25.0,
    "garage": 15.0,
    "garages": 15.0,
    "parking": 15.0,
    "shed": 10.0,
    "roof": 8.0,
    "service": 15.0,
    "kiosk": 5.0,
    "house": 15.0,
    "detached": 15.0,
    "hut": 8.0,
    "cabin": 8.0,
    "farm": 15.0,
    "barn": 15.0,
}


def sampled_height_cap(building_type: str | None) -> float | None:
    """A plausible ceiling for a sampled height, from the OSM ``building`` tag."""

    if not building_type:
        return None
    return SAMPLED_HEIGHT_CAPS_M.get(str(building_type).strip().lower())


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip()
    if not text:
        return None
    match = _NUMBER.search(text)
    if match is None:
        return None
    raw = match.group(0)
    if "," in raw and "." not in raw:
        raw = raw.replace(",", ".")
    try:
        number = float(raw)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_length_m(value: Any) -> float | None:
    """Parse a tag such as ``12``, ``12.5 m``, ``12,5`` or ``40 ft``.

    Returns ``None`` for missing, non-positive, or unparseable values.
    """

    number = _number(value)
    if number is None or number <= 0:
        return None
    text = str(value).strip().lower()
    if any(marker in text for marker in _FEET_MARKERS):
        number *= 0.3048
    return number


def parse_levels(value: Any) -> float | None:
    """Return the first level count found in a tag (e.g. ``3`` from ``3-5``)."""

    number = _number(value)
    if number is None or number <= 0:
        return None
    return number


@dataclass(frozen=True)
class HeightInfo:
    height_m: float
    base_m: float
    source: str
    clamped: bool = False


def resolve_height(
    tags: Mapping[str, Any],
    default_height_m: float = 25.0,
    level_height_m: float = 3.0,
) -> HeightInfo:
    """Resolve total height and base elevation from OSM tags.

    Priority: explicit ``height`` > ``building:levels`` (+ ``roof:height``) >
    configured default. ``min_height`` / ``building:min_level`` set the base.
    """

    clamped = False
    tag_height = parse_length_m(tags.get("height"))
    if tag_height is not None:
        height = tag_height
        source = "tag"
    else:
        levels = parse_levels(tags.get("building:levels"))
        if levels is not None:
            roof = parse_length_m(tags.get("roof:height")) or 0.0
            height = levels * level_height_m + roof
            source = "levels"
        else:
            height = float(default_height_m)
            source = "default"

    base = parse_length_m(tags.get("min_height")) or 0.0
    if base == 0.0:
        min_level = parse_levels(tags.get("building:min_level"))
        if min_level is not None:
            base = min_level * level_height_m

    if height > 300.0:
        height = 300.0
        clamped = True
    if height < 2.0:
        height = 2.0
        clamped = True
    if not math.isfinite(base) or base < 0.0:
        base = 0.0

    # A building must have a positive extrusion. If the base is too close to
    # the roof, lower the base rather than silently creating a zero-height mesh.
    if base > height - 0.5:
        base = max(0.0, height - 0.5)

    return HeightInfo(height_m=height, base_m=base, source=source, clamped=clamped)


@dataclass(frozen=True)
class KnownHeight:
    """One tagged observation: height, footprint area, and building type."""

    height_m: float
    area_m2: float = 0.0
    building_type: str = ""


@dataclass(frozen=True)
class SampledHeight:
    """A sampled height plus the rule that produced it."""

    height_m: float
    rule: str


class HeightSampler:
    """Draw plausible heights from the local OSM height distribution.

    Cities differ a lot: a Chongqing block mixes 6-storey walk-ups with 40-storey
    towers, a small town is nearly all 3-4 storeys. Giving every untagged
    building the same global default flattens that. Instead, sample the heights
    of the tagged buildings in the same selection, with a small jitter so the
    result does not repeat only a handful of exact values.

    Known heights may be paired with their footprint area and building type: a
    draw then weights every observation by how close its area is (log scale) and
    whether it shares the target's type or type group. OSM tags over-represent
    towers, so this keeps a shed from copying a skyscraper while leaving the
    selection's own shape intact.

    ``conservative`` adds an optional geometric plausibility bound,
    ``height <= slenderness_k * sqrt(area)``, which trims the slenderness tail
    for users who want a calmer skyline. It never raises a height.
    """

    def __init__(
        self,
        known: Iterable[KnownHeight | float | tuple[float, float] | tuple[float, float, str]],
        *,
        jitter: float = 0.1,
        seed: int = 0,
        conservative: bool = False,
        slenderness_k: float = 8.0,
    ) -> None:
        rows: list[tuple[float, float, str]] = []
        for item in known:
            if isinstance(item, KnownHeight):
                height_value, area_value, building_type = (
                    item.height_m,
                    item.area_m2,
                    item.building_type,
                )
            elif isinstance(item, (tuple, list)):
                height_value = item[0] if len(item) > 0 else 0.0
                area_value = item[1] if len(item) > 1 else 0.0
                building_type = item[2] if len(item) > 2 else ""
            else:
                height_value, area_value, building_type = item, 0.0, ""
            try:
                height = float(height_value)
                area = float(area_value)
            except (TypeError, ValueError):
                continue
            if not 2.0 <= height <= 300.0:
                continue
            rows.append(
                (
                    height,
                    area if math.isfinite(area) and area > 0.0 else 0.0,
                    str(building_type or "").strip(),
                )
            )

        rows.sort(key=lambda row: row[0])
        self._values = [row[0] for row in rows]
        self._areas = [row[1] for row in rows]
        self._types = [row[2] for row in rows]
        self._groups = [building_type_group(row[2]) for row in rows]
        self._jitter = max(0.0, float(jitter))
        self._rng = random.Random(int(seed))
        self._conservative = bool(conservative)
        self._slenderness_k = max(0.0, float(slenderness_k))

    def __len__(self) -> int:
        return len(self._values)

    @property
    def usable(self) -> bool:
        return bool(self._values)

    @property
    def area_weighted(self) -> bool:
        return any(area > 0.0 for area in self._areas)

    def _candidates(
        self,
        area_m2: float | None,
        building_type: str | None,
    ) -> tuple[list[int], list[float] | None, str]:
        """Candidate indexes, their weights (``None`` means uniform), and the rule."""

        everything = list(range(len(self._values)))
        target: float | None = None
        if area_m2 is not None and self.area_weighted:
            try:
                target = float(area_m2)
            except (TypeError, ValueError):
                target = None
            if target is not None and (not math.isfinite(target) or target <= 0.0):
                target = None
        if target is None:
            return everything, None, "pool"

        key = (building_type or "").strip().lower()
        group = building_type_group(building_type)
        log_target = math.log(target)
        indexes: list[int] = []
        weights: list[float] = []
        for index, (area, candidate_type, candidate_group) in enumerate(
            zip(self._areas, self._types, self._groups, strict=True)
        ):
            if area <= 0.0:
                continue
            delta = (math.log(area) - log_target) / AREA_BANDWIDTH_LOG
            weight = math.exp(-0.5 * delta * delta)
            if key and key not in _UNSPECIFIED_TYPES and candidate_type.lower() == key:
                weight *= TYPE_WEIGHT
            elif candidate_group == group:
                weight *= GROUP_WEIGHT
            if weight > 0.0:
                indexes.append(index)
                weights.append(weight)
        if not indexes or sum(weights) <= 0.0:
            return everything, None, "pool"
        return indexes, weights, "similarity"

    def draw(
        self,
        area_m2: float | None = None,
        building_type: str | None = None,
    ) -> SampledHeight:
        """One height in metres plus the rule that produced it."""

        if not self._values:
            raise ValueError("height sampler has no known heights")
        indexes, weights, rule = self._candidates(area_m2, building_type)
        if weights is None:
            base = self._values[self._rng.choice(indexes)]
        else:
            base = self._values[self._rng.choices(indexes, weights=weights, k=1)[0]]
        value = base * (1.0 + self._rng.uniform(-self._jitter, self._jitter))

        cap = sampled_height_cap(building_type)
        if cap is not None and value > cap:
            value = cap
            rule = "type_cap"
        if self._conservative and area_m2 is not None:
            try:
                target = float(area_m2)
            except (TypeError, ValueError):
                target = 0.0
            if target > 0.0:
                bound = self._slenderness_k * math.sqrt(target)
                if value > bound:
                    value = bound
                    rule = "conservative_cap"
        return SampledHeight(height_m=min(300.0, max(2.0, value)), rule=rule)
