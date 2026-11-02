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
# A known height is a candidate for a target footprint when its building area is
# between these fractions of the target area.
AREA_RATIO_MIN = 0.25
AREA_RATIO_MAX = 4.0


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


class HeightSampler:
    """Draw plausible heights from the local OSM height distribution.

    Cities differ a lot: a Chongqing block mixes 6-storey walk-ups with 40-storey
    towers, a small town is nearly all 3-4 storeys. Giving every untagged
    building the same global default flattens that. Instead, sample the heights
    of the tagged buildings in the same selection, with a small jitter so the
    result does not repeat only a handful of exact values.

    Known heights may be paired with their footprint area; a draw for a target
    footprint then prefers observations from similar-sized buildings (between
    ``area_ratio_min`` and ``area_ratio_max`` times its area), so a shed does not
    draw a tower height. When no observation falls in that band, the draw falls
    back to the whole pool.
    """

    def __init__(
        self,
        known: Iterable[float | tuple[float, float]],
        *,
        jitter: float = 0.1,
        seed: int = 0,
        area_ratio_min: float = AREA_RATIO_MIN,
        area_ratio_max: float = AREA_RATIO_MAX,
    ) -> None:
        heights: list[float] = []
        areas: list[float] = []
        for item in known:
            if isinstance(item, (tuple, list)):
                height_value, area_value = item[0], item[1]
            else:
                height_value, area_value = item, 0.0
            try:
                height = float(height_value)
                area = float(area_value)
            except (TypeError, ValueError):
                continue
            if not 2.0 <= height <= 300.0:
                continue
            heights.append(height)
            areas.append(area if math.isfinite(area) and area > 0.0 else 0.0)

        order = sorted(range(len(heights)), key=heights.__getitem__)
        self._values = [heights[index] for index in order]
        self._areas = [areas[index] for index in order]
        self._jitter = max(0.0, float(jitter))
        self._rng = random.Random(int(seed))
        self._area_ratio_min = max(0.0, float(area_ratio_min))
        self._area_ratio_max = max(self._area_ratio_min, float(area_ratio_max))

    def __len__(self) -> int:
        return len(self._values)

    @property
    def usable(self) -> bool:
        return bool(self._values)

    @property
    def area_weighted(self) -> bool:
        return any(area > 0.0 for area in self._areas)

    def _candidates(self, area_m2: float | None) -> list[float]:
        if area_m2 is None or not self.area_weighted:
            return self._values
        try:
            target = float(area_m2)
        except (TypeError, ValueError):
            return self._values
        if not math.isfinite(target) or target <= 0.0:
            return self._values
        low = target * self._area_ratio_min
        high = target * self._area_ratio_max
        pool = [
            height
            for height, area in zip(self._values, self._areas, strict=True)
            if area > 0.0 and low <= area <= high
        ]
        return pool or self._values

    def draw(self, area_m2: float | None = None) -> float:
        """One height in metres, jittered around a local observation."""

        if not self._values:
            raise ValueError("height sampler has no known heights")
        base = self._rng.choice(self._candidates(area_m2))
        value = base * (1.0 + self._rng.uniform(-self._jitter, self._jitter))
        return min(300.0, max(2.0, value))
