"""Report and check the sampled-height tail of a finished job.

Usage::

    python -m app.services.height_report --output scratch\\h3 --check

Reads ``metadata.json`` and ``buildings.geojson`` from a job directory (or its
``output/`` child) and prints the sampled tail, the rule counts, and the most
slender samples. ``--check`` turns the acceptance invariants into an exit code:
the sampled ``>=250 m`` share must not exceed the tagged pool's, no sampled
height may exceed its building-type ceiling, and a conservative run may not
exceed ``k * sqrt(area)``.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from app.services.heights import building_type_group, sampled_height_cap

# buildings.geojson rounds height and area to 1 mm, so a cap that was applied
# exactly can read back a fraction of a millimetre above its own bound.
_ROUNDING_TOLERANCE_M = 1e-3


def _share(part: int, total: int) -> float:
    return part / total if total else 0.0


def _shown(value: Any) -> Any:
    """Render a metadata value that older jobs do not have."""

    return "?" if value is None else value


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(quantile * (len(ordered) - 1))))
    return ordered[index]


def load_output(directory: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read ``metadata.json`` and ``buildings.geojson`` from a job directory."""

    base = Path(directory)
    if not (base / "metadata.json").is_file():
        base = base / "output"
    metadata = json.loads((base / "metadata.json").read_text(encoding="utf-8"))
    geojson = json.loads((base / "buildings.geojson").read_text(encoding="utf-8"))
    return metadata, list(geojson.get("features") or [])


def summarize(
    metadata: dict[str, Any],
    features: list[dict[str, Any]],
) -> dict[str, Any]:
    """Tail statistics for the sampled buildings and their tagged pool."""

    sampling = ((metadata or {}).get("buildings") or {}).get("height_sampling") or {}

    # One draw serves every part of a multi-polygon building, and it was drawn
    # from the summed footprint area: aggregate by OSM id so the slenderness
    # ratio is height / sqrt(total area), not height / sqrt(one part).
    buildings: dict[tuple[str, int], dict[str, Any]] = {}
    for feature in features:
        properties = feature.get("properties") or {}
        if properties.get("height_source") != "sampled":
            continue
        key = (str(properties.get("osm_type") or ""), int(properties.get("osm_id") or 0))
        entry = buildings.setdefault(
            key,
            {
                "osm_id": key[1],
                "height_m": float(properties.get("height_m") or 0.0),
                "area_m2": 0.0,
                "building": str((properties.get("tags") or {}).get("building") or ""),
                "height_rule": str(properties.get("height_rule") or "unknown"),
            },
        )
        entry["area_m2"] += float(properties.get("area_m2") or 0.0)
    sampled = list(buildings.values())

    heights = [float(entry.get("height_m") or 0.0) for entry in sampled]
    above_100 = sum(1 for height in heights if height >= 100.0)
    above_250 = sum(1 for height in heights if height >= 250.0)

    conservative = bool(sampling.get("conservative"))
    slenderness_k = float(sampling.get("slenderness_k") or 0.0)
    rules: Counter[str] = Counter()
    groups: dict[str, dict[str, Any]] = {}
    slenderness: list[dict[str, Any]] = []
    type_ceilings: list[dict[str, Any]] = []
    conservative_violations: list[dict[str, Any]] = []

    for entry in sampled:
        height = float(entry.get("height_m") or 0.0)
        area = float(entry.get("area_m2") or 0.0)
        building = str(entry.get("building") or "")
        osm_id = entry.get("osm_id")
        group = building_type_group(building)
        rules[str(entry.get("height_rule") or "unknown")] += 1

        bucket = groups.setdefault(
            group, {"count": 0, "heights": [], "above_100m": 0, "above_250m": 0}
        )
        bucket["count"] += 1
        bucket["heights"].append(height)
        if height >= 100.0:
            bucket["above_100m"] += 1
        if height >= 250.0:
            bucket["above_250m"] += 1

        if area > 0.0 and height > 0.0:
            ratio = height / math.sqrt(area)
            slenderness.append(
                {
                    "osm_id": osm_id,
                    "height_m": height,
                    "area_m2": area,
                    "ratio": ratio,
                    "building": building,
                }
            )
            bound = slenderness_k * math.sqrt(area)
            if conservative and slenderness_k > 0.0 and height > bound + _ROUNDING_TOLERANCE_M:
                conservative_violations.append(
                    {
                        "osm_id": osm_id,
                        "height_m": height,
                        "area_m2": area,
                        "bound_m": bound,
                        "building": building,
                    }
                )

        cap = sampled_height_cap(building)
        if cap is not None and height > cap + _ROUNDING_TOLERANCE_M:
            type_ceilings.append(
                {
                    "osm_id": osm_id,
                    "height_m": height,
                    "cap_m": cap,
                    "building": building,
                }
            )

    known = int(sampling.get("known") or 0)
    pool_above_100 = int(sampling.get("pool_above_100m") or 0)
    pool_above_250 = int(sampling.get("pool_above_250m") or 0)
    pool_recorded = "pool_above_100m" in sampling and "pool_above_250m" in sampling
    return {
        "sampled": {
            "count": len(sampled),
            "median_m": statistics.median(heights) if heights else 0.0,
            "p90_m": _percentile(heights, 0.9),
            "max_m": max(heights) if heights else 0.0,
            "above_100m": above_100,
            "above_100m_share": _share(above_100, len(sampled)),
            "above_250m": above_250,
            "above_250m_share": _share(above_250, len(sampled)),
        },
        "pool": {
            "known": known,
            "above_100m": pool_above_100,
            "above_100m_share": _share(pool_above_100, known),
            "above_250m": pool_above_250,
            "above_250m_share": _share(pool_above_250, known),
            "recorded": pool_recorded,
        },
        "metadata": {
            "enabled": sampling.get("enabled"),
            "reason": str(sampling.get("reason") or ""),
            "sampled": int(sampling.get("sampled") or 0),
            "sampled_buildings": sampling.get("sampled_buildings"),
            "sampled_above_100m": sampling.get("sampled_above_100m"),
            "sampled_above_250m": sampling.get("sampled_above_250m"),
            "band_fallback_draws": sampling.get("band_fallback_draws"),
        },
        "rules": dict(sorted(rules.items())),
        "caps": {
            "type_capped": int(sampling.get("type_capped") or 0),
            "conservative": conservative,
            "slenderness_k": slenderness_k,
        },
        "groups": {
            group: {
                "count": bucket["count"],
                "median_m": statistics.median(bucket["heights"]),
                "above_100m": bucket["above_100m"],
                "above_250m": bucket["above_250m"],
            }
            for group, bucket in sorted(groups.items())
        },
        "slenderness": slenderness,
        "type_ceilings": type_ceilings,
        "conservative_violations": conservative_violations,
    }


def check(summary: dict[str, Any]) -> list[str]:
    """The acceptance invariants, as human-readable violations (empty when clean)."""

    violations: list[str] = []
    sampled = summary["sampled"]
    pool = summary["pool"]
    if (
        pool["recorded"]
        and sampled["count"]
        and sampled["above_250m_share"] > pool["above_250m_share"] + 1e-9
    ):
        violations.append(
            "sampled heights >=250 m: "
            f"{sampled['above_250m']} of {sampled['count']} ({sampled['above_250m_share']:.1%}) "
            f"exceed the tagged pool's {pool['above_250m']} of {pool['known']} "
            f"({pool['above_250m_share']:.1%})"
        )
    for item in summary["type_ceilings"]:
        violations.append(
            f"OSM {item['osm_id']} ({item['building'] or 'unspecified'}) sampled at "
            f"{item['height_m']:.1f} m exceeds its {item['cap_m']:g} m type ceiling"
        )
    for item in summary["conservative_violations"]:
        violations.append(
            f"OSM {item['osm_id']} ({item['building'] or 'unspecified'}) sampled at "
            f"{item['height_m']:.1f} m exceeds the slenderness bound {item['bound_m']:.1f} m "
            f"({item['area_m2']:.0f} m2)"
        )
    return violations


def format_report(summary: dict[str, Any]) -> str:
    """A short text report for a terminal."""

    sampled = summary["sampled"]
    pool = summary["pool"]
    caps = summary["caps"]
    lines = [
        "Height sampling report",
        "======================",
        f"sampled buildings   {sampled['count']}",
        f"tagged pool         {pool['known']} observations",
        f"sampled heights     median {sampled['median_m']:.1f} m, "
        f"p90 {sampled['p90_m']:.1f} m, max {sampled['max_m']:.1f} m",
        "",
        "tail                sampled                 pool",
        f"  >=100 m           {sampled['above_100m']:>4} ({sampled['above_100m_share']:5.1%})"
        f"          {pool['above_100m']:>4} ({pool['above_100m_share']:5.1%})",
        f"  >=250 m           {sampled['above_250m']:>4} ({sampled['above_250m_share']:5.1%})"
        f"          {pool['above_250m']:>4} ({pool['above_250m_share']:5.1%})",
    ]
    if not pool["recorded"]:
        lines.append("pool tail           not recorded (pre-v3 job): share check skipped")
    metadata = summary.get("metadata") or {}
    if metadata.get("enabled") is False:
        reason = metadata.get("reason") or "unknown"
        lines.append(f"sampling            disabled ({reason}); no heights were sampled")
    if metadata.get("sampled"):
        lines.append(
            f"metadata            {metadata['sampled']} parts / "
            f"{_shown(metadata.get('sampled_buildings'))} buildings sampled; tail "
            f">=100 m {_shown(metadata.get('sampled_above_100m'))}, "
            f">=250 m {_shown(metadata.get('sampled_above_250m'))}, "
            f"band fallbacks {_shown(metadata.get('band_fallback_draws'))}"
        )
    if summary["rules"]:
        rules = ", ".join(f"{name} {count}" for name, count in summary["rules"].items())
        lines.append(f"rules               {rules}")
    lines.append(
        f"caps                type ceilings {caps['type_capped']}, "
        f"conservative {caps['conservative']} (k={caps['slenderness_k']:g})"
    )
    if summary["groups"]:
        lines += ["", "by type group (sampled):"]
        for group, info in summary["groups"].items():
            lines.append(
                f"  {group:<12} n={info['count']:>4}  median {info['median_m']:6.1f} m  "
                f">=100 m {info['above_100m']:>4}  >=250 m {info['above_250m']:>3}"
            )
    if summary["slenderness"]:
        lines += ["", "most slender samples (height / sqrt(area)):"]
        for item in sorted(summary["slenderness"], key=lambda entry: -entry["ratio"])[:5]:
            lines.append(
                f"  {item['height_m']:6.1f} m on {item['area_m2']:7.0f} m2  "
                f"ratio {item['ratio']:4.1f}  OSM {item['osm_id']} "
                f"({item['building'] or 'unspecified'})"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="height-report",
        description="Report (and optionally check) the sampled-height tail of a job.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="job directory containing output/, or the output/ directory itself",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when an acceptance invariant fails",
    )
    args = parser.parse_args(argv)

    metadata, features = load_output(args.output)
    summary = summarize(metadata, features)
    print(format_report(summary))
    violations = check(summary)
    if not violations:
        print("\nOK: the sampled tail passes the acceptance checks")
        return 0
    print("\nFAILED:")
    for line in violations:
        print(f"- {line}")
    return 1 if args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
