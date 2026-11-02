"""Headless command line entry point for the generation pipeline.

The HTTP layer is only a wrapper: ``run_pipeline`` needs a request, a settings
object, and a job directory. This module exposes that directly so scans can be
run in a terminal, a batch script, or a cluster job without uvicorn.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from app.config import Settings
from app.models.schemas import BBox, GenerateRequest
from app.services.errors import CityGeneratorError
from app.services.pipeline import PipelineResult, run_pipeline


def parse_bbox(value: str) -> BBox:
    """Parse ``south,west,north,east`` in WGS84 degrees."""

    parts = [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be south,west,north,east")
    try:
        south, west, north, east = (float(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"bbox contains a non-number: {exc}") from exc
    try:
        return BBox(south=south, west=west, north=north, east=east)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="citygen",
        description="Generate a watertight CityGenerator STL without the web UI.",
    )
    parser.add_argument(
        "--bbox",
        required=True,
        type=parse_bbox,
        help="south,west,north,east in WGS84 degrees (e.g. 29.553,106.5467,29.5891,106.588)",
    )
    parser.add_argument("--out", required=True, type=Path, help="output (job) directory")
    parser.add_argument("--height", type=float, default=25.0, help="default building height, m")
    parser.add_argument("--level-height", type=float, default=3.0, help="storey height, m")
    parser.add_argument("--embed", type=float, default=0.5, help="bottom embed depth, m")
    parser.add_argument("--ground-margin", type=float, default=0.0, help="base margin, m")
    parser.add_argument("--simplify", type=float, default=0.05, help="footprint simplification, m")
    parser.add_argument("--min-area", type=float, default=4.0, help="minimum footprint area, m2")
    parser.add_argument("--terrain-resolution", type=float, default=30.0, help="DEM grid spacing, m")
    parser.add_argument("--no-terrain", action="store_true", help="use a flat 1 m slab instead of the DEM")
    parser.add_argument("--refresh", action="store_true", help="ignore the Overpass cache")
    parser.add_argument("--data-dir", type=Path, default=None, help="cache/job root (default: ./data)")
    parser.add_argument("--quiet", action="store_true", help="only print the final summary")
    return parser


def _progress_printer(quiet: bool):
    state = {"key": None}

    def report(percent: int, stage: str, message: str) -> None:
        if quiet:
            return
        key = (stage, percent // 5)
        if key == state["key"]:
            return
        state["key"] = key
        print(f"[{percent:3d}%] {stage}: {message}", flush=True)

    return report


def _summary(result: PipelineResult, job_dir: Path) -> None:
    stats = result.stats
    print("")
    print(f"buildings      {stats['buildings']}")
    print(f"triangles      {stats['triangles']} (terrain {stats['terrain_triangles']})")
    print(f"watertight     {stats['city_watertight']}")
    print(f"weld repairs   {stats['weld_repairs']}")
    print(f"area           {stats['area_km2']} km²")
    if stats["terrain_enabled"]:
        print(f"terrain relief {stats['terrain_relief_m']} m @ {stats['terrain_resolution_m']} m")
    for warning in result.warnings:
        print(f"warning        [{warning.code}] {warning.message}")
    print("")
    print(f"output         {(job_dir / 'output').resolve()}")
    print(f"zip            {result.zip_path.resolve()}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings(data_dir=args.data_dir) if args.data_dir else Settings()
    request = GenerateRequest(
        bbox=args.bbox,
        options={
            "default_height_m": args.height,
            "level_height_m": args.level_height,
            "embed_depth_m": args.embed,
            "ground_margin_m": args.ground_margin,
            "simplify_m": args.simplify,
            "min_building_area_m2": args.min_area,
            "terrain": {
                "enabled": not args.no_terrain,
                "resolution_m": args.terrain_resolution,
            },
        },
        refresh=args.refresh,
    )
    started = time.time()
    try:
        result = run_pipeline(
            request,
            settings,
            args.out,
            progress_cb=_progress_printer(args.quiet),
        )
    except CityGeneratorError as exc:
        print(f"error [{exc.code}] {exc.message}", file=sys.stderr)
        if exc.detail:
            print(f"       {exc.detail}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    if not args.quiet:
        print(f"done in {time.time() - started:.1f}s", flush=True)
    _summary(result, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
