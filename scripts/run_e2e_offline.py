"""Run the LEO coverage-risk pipeline end to end **without an API key**.

The agentic pipeline (``python -m leo_pipeline.orchestrator --run``) drives A1–A5 through
the Claude Agent SDK and needs ``ANTHROPIC_API_KEY``. This script exercises the *same*
deterministic geo engines on a single tile with **no LLM in the loop** — useful for CI, for
a quick demo, or whenever the API budget is unavailable. Every stage below calls the exact
code the agent would call; only the agent's *orchestration/judgement* is replaced by this
fixed driver.

Pipeline (see ``docs/architecture.md`` for the agent roles):

    pre-steps   ingest + tiling .................... already on disk in data/interim/
    A2 surface  build one aligned DSM for the tile .. THIS SCRIPT (no-API gap filler)
    A3 analysis run_analysis --compute .............. per-location obstruction % + risk tier
    A4 QA       run_qa       --compute .............. input audit + output-anomaly scan
    A5 report   run_report   --compute .............. county/state rollup + PMTiles map + log

Why this script exists: A3/A4/A5 each ship a ``--compute`` no-API path, but **A2 does not**
— its CLI (``run_ingestion``) only runs the surface build through the agent. So the single
missing piece for a fully offline run is "turn a tile bbox into a cached surface GeoTIFF".
The ``build_surface`` function below does exactly that, mirroring the A2 agent's fallback
hierarchy (true lidar DSM → DEM-only ``cover_proxy``) deterministically.

Network is still required (the surface is a windowed read of a public COG over HTTPS); only
the *LLM* calls are removed.

Usage
-----
    ../../.venv/bin/python scripts/run_e2e_offline.py                  # default demo tile
    ../../.venv/bin/python scripts/run_e2e_offline.py --tile 32617_158_806
    ../../.venv/bin/python scripts/run_e2e_offline.py --no-tiles       # skip tippecanoe PMTiles
"""

from __future__ import annotations

import argparse
import asyncio

from leo_pipeline import run_analysis, run_qa, run_report
from leo_pipeline.config import PATHS
# Surface-build logic lives in leo_pipeline.offline (one source of truth, shared with the
# parallel full-run driver leo_pipeline.run_full).
from leo_pipeline.offline import DEFAULT_TILE, build_surface, tile_bbox


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--tile", default=DEFAULT_TILE, help=f"work-list tile_id (default {DEFAULT_TILE})")
    p.add_argument("--no-tiles", action="store_true", help="skip the tippecanoe PMTiles step in A5")
    args = p.parse_args(argv)

    bbox = tile_bbox(args.tile)
    print(f"\n=== A2 surface build · tile {args.tile} · bbox {bbox} ===")
    asyncio.run(build_surface(args.tile, bbox))

    print(f"\n=== A3 analysis (--compute) ===")
    run_analysis.main(["--tile", args.tile, "--compute"])

    # Whole-run QA (no --tile) writes qa_report.json, which is the filename A5 reads to fold
    # anomalies into the decision log. A per-tile run writes qa_report_<tile>.json instead,
    # which A5's _load_anomalies() does not currently look for.
    print(f"\n=== A4 QA (--compute) ===")
    run_qa.main(["--compute"])

    print(f"\n=== A5 report (--compute) ===")
    report_argv = ["--compute"] + (["--no-tiles"] if args.no_tiles else [])
    run_report.main(report_argv)

    print(f"\nDone. Artifacts in {PATHS.root / 'outputs'}/ (aggregates.json, locations.geojson, "
          "locations.pmtiles, coverage_map.html, decision_log.md)")


if __name__ == "__main__":
    main()
