"""Run the A3 sky-obstruction analysis agent on its own, from one command.

Wraps ``GEO_ANALYSIS_AGENT`` (A3) in a minimal Claude Agent SDK harness so a user can turn
the A2-built per-tile surfaces into per-location obstruction scores + risk tiers without
hand-assembling options. It mirrors ``run_ingestion``: pick the tiles from the A2 surface
cache, join the unique-coordinate work list to each tile, and score. The agent chooses the
parameters (mount-height sweep, edge handling); the per-azimuth horizon math is deterministic
(``leo_pipeline.horizon``).

Two paths:

- default (agentic): drive A3 — it batches each tile's points through compute_sky_obstruction
  and reasons about edges/mount height. Needs ANTHROPIC_API_KEY.
- ``--compute``: run the SAME deterministic horizon engine directly (no agent, no API),
  writing a per-tile findings JSON and a tier summary. The offline/verification path.

Examples
--------
    # Score the first few cached-surface tiles with the agent:
    python -m leo_pipeline.run_analysis --limit 3

    # Deterministically score one tile's points offline and write findings:
    python -m leo_pipeline.run_analysis --tile 32617_120_3940 --compute

    # Score a single ad-hoc point against a specific surface (offline):
    python -m leo_pipeline.run_analysis --point 35.5 -80.5 --dsm-uri data/interim/surfaces/T1__ab_dsm.tif --compute

    # Show what it would do, no API call:
    python -m leo_pipeline.run_analysis --limit 3 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
from pathlib import Path

import leo_pipeline.tools as tools
from leo_pipeline.agents import GEO_ANALYSIS_AGENT
from leo_pipeline.config import ANALYSIS, MODELS, PATHS, require_api_key
from leo_pipeline.ingest import UNIQUE_COORDS_FILE
from leo_pipeline.tiling import COORD_TILE_MAP_FILE

# A3 reads only the leo analysis tools — never STAC, never the open web, never the SQL surface.
A3_TOOL_NAMES = [
    "mcp__leo__compute_sky_obstruction",
    "mcp__leo__find_clear_sky_spot",
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leo-analysis",
        description="Run the A3 analysis agent and score per-location sky obstruction.",
    )
    p.add_argument("--tile", help="Score a single tile_id from the surface cache.")
    p.add_argument(
        "--point",
        nargs=2,
        type=float,
        metavar=("LAT", "LON"),
        help="Score one ad-hoc point (EPSG:4326); needs --dsm-uri for the surface.",
    )
    p.add_argument("--dsm-uri", help="Surface GeoTIFF to score --point against.")
    p.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Max cached-surface tiles to score (default %(default)s; ignored with --tile/--point).",
    )
    p.add_argument(
        "--mount-height",
        type=float,
        help="Dish mount height (m) above the surface (default 2.0; rationale sweeps {0,1.5,3}).",
    )
    p.add_argument(
        "--theta",
        type=float,
        help="Override the minimum reception angle θ in degrees (default from the spec, 25).",
    )
    p.add_argument("--cache-dir", help="Override the A2 surface cache directory to read from.")
    p.add_argument(
        "--compute",
        action="store_true",
        help="Deterministic path: run the horizon engine directly (no agent, no API) and "
        "write per-tile findings.",
    )
    p.add_argument(
        "--model",
        default=MODELS.driver,
        help="Driver model that orchestrates the agent (default: %(default)s).",
    )
    p.add_argument("--quiet", action="store_true", help="Suppress the streaming agent output.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve the surfaces + points and print the plan, then exit (no API call).",
    )
    return p


def load_surfaces(cache_dir: Path) -> dict[str, dict]:
    """Map tile_id -> the A2 surface payload (dsm_uri, confidence, ...) from the cache."""
    surfaces: dict[str, dict] = {}
    if not cache_dir.exists():
        return surfaces
    for sidecar in sorted(cache_dir.glob("*.json")):
        try:
            payload = json.loads(sidecar.read_text())
        except Exception:
            continue
        tile_id = payload.get("tile_id")
        if tile_id and payload.get("dsm_uri"):
            # Last write wins; sidecars are content-addressed so a tile's latest surface is fine.
            surfaces[tile_id] = payload
    return surfaces


def points_for_tile(tile_id: str) -> list[dict]:
    """Unique-coordinate work items on a tile: join coord_tile_map → unique_coords.

    A3 scores the de-duplicated work list (one job per unique coordinate); the fan-out back to
    real location_ids reuses location_coord_map downstream. Returns [{location_id, lat, lon}]
    where location_id is the stable ``coord_<coord_id>`` work-item id.
    """
    import pandas as pd

    map_path = PATHS.data_interim / COORD_TILE_MAP_FILE
    coords_path = PATHS.data_interim / UNIQUE_COORDS_FILE
    if not map_path.exists() or not coords_path.exists():
        return []
    cmap = pd.read_parquet(map_path)
    on_tile = cmap[cmap["tile_id"] == tile_id]
    if on_tile.empty:
        return []
    coords = pd.read_parquet(coords_path, columns=["coord_id", "latitude", "longitude"])
    joined = on_tile.merge(coords, on="coord_id", how="inner")
    return [
        {
            "location_id": f"coord_{int(r.coord_id)}",
            "lat": float(r.latitude),
            "lon": float(r.longitude),
        }
        for r in joined.itertuples()
    ]


def resolve_work(args: argparse.Namespace) -> list[dict]:
    """Decide what to score: an ad-hoc --point, a single --tile, or the cached-surface list.

    Returns a list of {tile_id, dsm_uri, confidence, points} batches.
    """
    if args.point:
        if not args.dsm_uri:
            raise SystemExit("--point requires --dsm-uri (the surface to score it against)")
        dsm = Path(args.dsm_uri).expanduser().resolve()
        if not dsm.exists():
            raise SystemExit(f"--dsm-uri not found: {dsm}")
        lat, lon = args.point
        return [
            {
                "tile_id": "adhoc_point",
                "dsm_uri": str(dsm),
                "confidence": None,
                "points": [{"location_id": "adhoc_point", "lat": lat, "lon": lon}],
            }
        ]

    surfaces = load_surfaces(tools.INGESTION.cache_dir)
    if not surfaces:
        raise SystemExit(
            f"No A2 surfaces in {tools.INGESTION.cache_dir}. Run the ingestion agent first "
            "(python -m leo_pipeline.run_ingestion), or pass --point + --dsm-uri."
        )
    if args.tile:
        if args.tile not in surfaces:
            raise SystemExit(f"tile_id {args.tile!r} has no cached surface")
        tile_ids = [args.tile]
    else:
        tile_ids = list(surfaces)[: max(1, args.limit)]

    work = []
    for tid in tile_ids:
        s = surfaces[tid]
        work.append(
            {
                "tile_id": tid,
                "dsm_uri": s["dsm_uri"],
                "confidence": s.get("confidence"),
                "points": points_for_tile(tid),
            }
        )
    return work


def prepare(args: argparse.Namespace) -> dict:
    """Apply the cache-dir override and resolve the work batches + the active sky spec."""
    if args.cache_dir:
        cache = Path(args.cache_dir).expanduser().resolve()
        tools.INGESTION = dataclasses.replace(tools.INGESTION, cache_dir=cache)
    work = resolve_work(args)
    sky_spec = {}
    if args.theta is not None:
        sky_spec["min_elev_deg"] = args.theta
    return {
        "work": work,
        "sky_spec": sky_spec,
        "dish_height_m": 2.0 if args.mount_height is None else args.mount_height,
        "cache_dir": tools.INGESTION.cache_dir,
    }


# --- deterministic path (--compute): run the horizon engine directly -----------------


def compute_findings(info: dict) -> list[dict]:
    """Score every batch deterministically with leo_pipeline.horizon and write per-tile
    findings JSON to ANALYSIS.findings_dir. The no-API verification/CI path; the agentic
    default calls the same engine via the compute_sky_obstruction tool."""
    from leo_pipeline import horizon
    from leo_pipeline.tools import _resolve_sky_spec, _surface_confidence_for

    spec = _resolve_sky_spec({"sky_spec": info["sky_spec"]})
    out_dir = ANALYSIS.findings_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    dish_h = float(info["dish_height_m"])

    all_findings: list[dict] = []
    for batch in info["work"]:
        if not batch["points"]:
            continue
        sampler = horizon._cached_sampler(batch["dsm_uri"])
        surface_conf = batch.get("confidence") or _surface_confidence_for(batch["dsm_uri"])
        rows = []
        for p in batch["points"]:
            score = horizon.score_point(sampler, p["lon"], p["lat"], dish_h, spec)
            conf = horizon.confidence_flag(
                surface_conf, score.get("sampled_fraction", 0.0), score.get("obstruction_pct") or 0.0, spec
            )
            rows.append(
                {
                    "location_id": p["location_id"],
                    "tile_id": batch["tile_id"],
                    "obstruction_pct": score["obstruction_pct"],
                    "risk_tier": score["risk_tier"],
                    "confidence": conf,
                    "dish_height_m": score["dish_height_m"],
                    "blocked_azimuths": score["blocked_azimuths"],
                    "dsm_uri": batch["dsm_uri"],
                    "spec_version": ANALYSIS.spec_version,
                }
            )
        out_path = out_dir / f"findings_{batch['tile_id']}.json"
        out_path.write_text(json.dumps(rows, indent=2, default=str))
        all_findings.extend(rows)
    return all_findings


def _print_tier_summary(findings: list[dict]) -> None:
    tiers: dict[str, int] = {}
    for f in findings:
        tiers[f["risk_tier"]] = tiers.get(f["risk_tier"], 0) + 1
    print(f"\nScored {len(findings)} locations -> {ANALYSIS.findings_dir}")
    for tier in ("clear", "at_risk", "severe", "undetermined"):
        if tier in tiers:
            print(f"  {tier:13} {tiers[tier]}")


# --- agentic path --------------------------------------------------------------------


def build_analysis_options(model: str):
    """A focused ClaudeAgentOptions registering only the A3 analysis agent."""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        model=model,
        mcp_servers={"leo": tools.LEO_TOOLS_SERVER},
        allowed_tools=A3_TOOL_NAMES,
        agents={"geo-analysis": GEO_ANALYSIS_AGENT},
        cwd=str(PATHS.root),
        system_prompt=(
            "Delegate to the geo-analysis subagent to score sky obstruction for each tile's "
            "locations against its A2 surface — batch each tile's points through "
            "compute_sky_obstruction, sweep mount height for at-risk points — and stop."
        ),
    )


def _build_query(info: dict) -> str:
    """The task message: each tile's surface + its points + the active spec overrides."""
    batches = [
        {"tile_id": b["tile_id"], "dsm_uri": b["dsm_uri"], "n_points": len(b["points"]),
         "points": b["points"]}
        for b in info["work"]
    ]
    lines = [
        "Score sky obstruction for these tiles. For each, call compute_sky_obstruction once "
        "with the tile's dsm_uri and ALL its points; classify each location's risk tier.",
        "",
        f"Use dish_height_m={info['dish_height_m']} for the first pass; re-score at_risk/severe "
        "locations at a higher mount height to report 'clear if raised'.",
        "",
        "Tiles to score (tile_id + dsm_uri + points):",
        json.dumps(batches, indent=2),
    ]
    if info["sky_spec"]:
        lines.append(f"\nApply these sky_spec overrides: {json.dumps(info['sky_spec'])}")
    return "\n".join(lines)


def _render(message) -> None:
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return
    for block in content:
        name = getattr(block, "name", None)
        text = getattr(block, "text", None)
        if name:
            print(f"  -> tool: {name}")
        elif text:
            print(text)


async def _run(options, query: str, verbose: bool) -> None:
    from claude_agent_sdk import ClaudeSDKClient

    async with ClaudeSDKClient(options=options) as client:
        await client.query(query)
        async for message in client.receive_response():
            if verbose:
                _render(message)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    info = prepare(args)

    n_pts = sum(len(b["points"]) for b in info["work"])
    print(f"Surfaces   : {len(info['work'])} ({[b['tile_id'] for b in info['work']]})")
    print(f"Points     : {n_pts}")
    print(f"Dish height: {info['dish_height_m']} m")
    print(f"Spec       : {ANALYSIS.spec_version}"
          + (f" (overrides: {info['sky_spec']})" if info["sky_spec"] else ""))
    print(f"Cache dir  : {info['cache_dir']}")
    if args.dry_run:
        print("(dry run — not calling the API)")
        return

    if args.compute:
        findings = compute_findings(info)
        _print_tier_summary(findings)
        return

    print(f"Model      : {args.model}")
    require_api_key()
    options = build_analysis_options(args.model)
    query = _build_query(info)
    asyncio.run(_run(options, query, verbose=not args.quiet))


if __name__ == "__main__":
    main()
