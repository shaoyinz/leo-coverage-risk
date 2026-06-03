"""Run the A2 surface-ingestion agent on its own, from one command.

Wraps ``INGESTION_AGENT`` (A2) in a minimal Claude Agent SDK harness so a user can turn the
H1-approved data manifest into aligned per-tile surfaces without hand-assembling options.
It mirrors ``run_discovery``: pick the tiles to ingest from the ``tiles.parquet`` work list
(produced by ``python -m leo_pipeline.tiling``), or target an ad-hoc area with ``--bbox``;
cap the run with ``--limit`` to bound cost. The agent does the real work — resolve each
approved dataset to a concrete COG for the tile, then read/reproject/fuse the surface.

Examples
--------
    # Ingest the first few tiles of the work list (default manifest + tiles):
    python -m leo_pipeline.run_ingestion --limit 3

    # Ingest one ad-hoc area as a single tile:
    python -m leo_pipeline.run_ingestion --bbox -80.0 35.0 -79.95 35.05

    # Show what it would do, no API call:
    python -m leo_pipeline.run_ingestion --limit 3 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
from pathlib import Path

import leo_pipeline.tools as tools
from leo_pipeline.agents import INGESTION_AGENT
from leo_pipeline.config import MODELS, PATHS, require_api_key
from leo_pipeline.tiling import TILES_FILE

# A2 reads only the leo surface tools — never the open web, never the locations SQL.
A2_TOOL_NAMES = [
    "mcp__leo__cache_rw",
    "mcp__leo__stac_item_read",
    "mcp__leo__fetch_aligned_surface",
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leo-ingestion",
        description="Run the A2 surface-ingestion agent and build aligned per-tile surfaces.",
    )
    p.add_argument(
        "--manifest",
        help="Path to the H1-approved data manifest JSON "
        "(default: data/interim/data_manifest.json).",
    )
    p.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help="Ingest this ad-hoc area as a single tile (EPSG:4326); overrides tiles.parquet.",
    )
    p.add_argument("--tile", help="Ingest a single tile_id from tiles.parquet.")
    p.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Max tiles to ingest from the work list (default %(default)s; ignored with "
        "--bbox/--tile).",
    )
    p.add_argument(
        "--gsd", type=float, help="Override target output resolution in metres (default 10)."
    )
    p.add_argument(
        "--surface-mode",
        choices=["true_dsm", "pseudo_dsm", "cover_proxy"],
        help="Force a surface mode instead of letting the agent walk the fallback hierarchy.",
    )
    p.add_argument("--cache-dir", help="Override the surface cache directory.")
    p.add_argument(
        "--model",
        default=MODELS.driver,
        help="Driver model that orchestrates the agent (default: %(default)s).",
    )
    p.add_argument("--quiet", action="store_true", help="Suppress the streaming agent output.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve the manifest + tile list and print the plan, then exit (no API call).",
    )
    return p


def _adhoc_tile_id(bbox: list[float]) -> str:
    """Stable tile_id for an ad-hoc --bbox area (so re-runs reuse the same cache entry)."""
    h = hashlib.sha1(json.dumps([round(x, 6) for x in bbox]).encode()).hexdigest()[:8]
    return f"adhoc_{h}"


def load_manifest(path: Path) -> dict:
    """Read the manifest and summarise the SELECTED dataset per obstruction factor.

    Returns ``{aoi_bbox, factors: {factor: {dataset_id, access, collection, source_url,
    asset_href, license}}}`` — exactly what the agent needs to resolve each factor to a
    concrete COG (a STAC collection to query, or an off-catalog source_url).
    """
    data = json.loads(path.read_text())
    factors: dict[str, dict] = {}
    for e in data.get("entries", []):
        if not e.get("selected"):
            continue
        factors[e["factor"]] = {
            "dataset_id": e.get("dataset_id"),
            "access": e.get("access"),
            "collection": e.get("collection") or e.get("dataset_id"),
            "source_url": e.get("source_url") or e.get("asset_href"),
            "asset_href": e.get("asset_href"),
            "license": e.get("license"),
        }
    return {"aoi_bbox": data.get("aoi_bbox"), "factors": factors}


def resolve_tiles(args: argparse.Namespace) -> list[dict]:
    """Decide which tiles to ingest: an ad-hoc --bbox, a single --tile, or the work list."""
    if args.bbox:
        lon0, lat0, lon1, lat1 = args.bbox
        if not (lon0 < lon1 and lat0 < lat1):
            raise SystemExit("--bbox must be MIN_LON MIN_LAT MAX_LON MAX_LAT with min < max")
        return [{"tile_id": _adhoc_tile_id(list(args.bbox)), "bbox": list(args.bbox)}]

    tiles_path = PATHS.data_interim / TILES_FILE
    if not tiles_path.exists():
        raise SystemExit(
            f"No tile work list at {tiles_path}. Run `python -m leo_pipeline.tiling` first, "
            "or pass --bbox to ingest an ad-hoc area."
        )
    import pandas as pd

    df = pd.read_parquet(tiles_path)
    if args.tile:
        row = df[df["tile_id"] == args.tile]
        if row.empty:
            raise SystemExit(f"tile_id {args.tile!r} not found in {tiles_path}")
        df = row
    else:
        df = df.head(max(1, args.limit))
    return [
        {
            "tile_id": r.tile_id,
            "bbox": [float(r.min_lon), float(r.min_lat), float(r.max_lon), float(r.max_lat)],
        }
        for r in df.itertuples()
    ]


def prepare(args: argparse.Namespace) -> dict:
    """Validate inputs, apply cache-dir override, resolve the manifest + tile list."""
    if args.cache_dir:
        cache = Path(args.cache_dir).expanduser().resolve()
        tools.INGESTION = dataclasses.replace(tools.INGESTION, cache_dir=cache)

    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else tools.DISCOVERY.manifest_path
    )
    if not manifest_path.exists():
        raise SystemExit(
            f"Approved data manifest not found at {manifest_path}. Run the A1 "
            "data-discovery agent first (python -m leo_pipeline.run_discovery)."
        )
    manifest = load_manifest(manifest_path)
    tiles = resolve_tiles(args)
    return {
        "manifest_path": manifest_path,
        "manifest": manifest,
        "tiles": tiles,
        "cache_dir": tools.INGESTION.cache_dir,
    }


def build_ingestion_options(model: str):
    """A focused ClaudeAgentOptions registering only the A2 surface-ingestion agent."""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        model=model,
        mcp_servers={"leo": tools.LEO_TOOLS_SERVER},
        allowed_tools=A2_TOOL_NAMES,
        agents={"ingestion": INGESTION_AGENT},
        cwd=str(PATHS.root),
        system_prompt=(
            "Delegate to the ingestion subagent to build one aligned surface per tile from "
            "the approved manifest — check the cache, resolve each dataset to a COG for the "
            "tile, then fetch/reproject/fuse — and stop."
        ),
    )


def _build_query(info: dict, surface_mode: str | None, gsd: float | None) -> str:
    """The task message handed to the agent: the manifest factors + the tiles to ingest."""
    lines = [
        "Build aligned surfaces for these tiles using the approved data manifest.",
        "",
        "Approved datasets (factor -> how to resolve a COG for a tile):",
        json.dumps(info["manifest"]["factors"], indent=2),
        "",
        "Tiles to ingest (tile_id + bbox in EPSG:4326):",
        json.dumps(info["tiles"], indent=2),
    ]
    if surface_mode:
        lines.append(f"\nForce surface_mode={surface_mode!r} for every tile.")
    if gsd:
        lines.append(f"Use target_gsd_m={gsd} for every tile.")
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


def _summarize_cache(cache_dir: Path, tiles: list[dict]) -> None:
    tile_ids = {t["tile_id"] for t in tiles}
    found = []
    if cache_dir.exists():
        for sidecar in sorted(cache_dir.glob("*.json")):
            try:
                payload = json.loads(sidecar.read_text())
            except Exception:
                continue
            if payload.get("tile_id") in tile_ids:
                found.append(payload)

    print(f"\nSurfaces in cache: {cache_dir}")
    print(f"  tiles requested : {len(tile_ids)}")
    print(f"  surfaces present: {len(found)}")
    for p in found:
        print(
            f"   - {p.get('tile_id'):20} {p.get('surface_mode'):11} "
            f"[{p.get('confidence')}/{p.get('coverage_flag')}] gsd={p.get('gsd_m')} "
            f"-> {p.get('dsm_uri')}"
        )
    missing = tile_ids - {p.get("tile_id") for p in found}
    if missing:
        print(f"  tiles MISSING a surface: {sorted(missing)}")


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    info = prepare(args)

    print(f"Manifest   : {info['manifest_path']}")
    print(f"Factors    : {sorted(info['manifest']['factors'])}")
    print(f"Tiles      : {len(info['tiles'])} ({[t['tile_id'] for t in info['tiles']]})")
    print(f"Cache dir  : {info['cache_dir']}")
    print(f"Model      : {args.model}")
    if args.dry_run:
        print("(dry run — not calling the API)")
        return

    require_api_key()
    options = build_ingestion_options(args.model)
    query = _build_query(info, args.surface_mode, args.gsd)
    asyncio.run(_run(options, query, verbose=not args.quiet))
    _summarize_cache(info["cache_dir"], info["tiles"])


if __name__ == "__main__":
    main()
