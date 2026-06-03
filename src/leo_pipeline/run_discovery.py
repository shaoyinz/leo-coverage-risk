"""Run the A1 data-discovery agent on its own, from one command.

Wraps ``DATA_DISCOVERY_AGENT`` in a minimal Claude Agent SDK harness so a user can
produce a ranked obstruction-data manifest without hand-assembling options. It closes the
two standalone-usage gaps: pick a custom AOI with ``--bbox`` (instead of needing the
bundled locations CSV) and choose where the manifest lands with ``--out``. The agent still
does the real work — search STAC + the web, rank, and write the manifest for the H1 gate.

Examples
--------
    # Derive the AOI from the bundled locations CSV (default output path):
    python -m leo_pipeline.run_discovery

    # Target your own area and write to a chosen path:
    python -m leo_pipeline.run_discovery \\
        --bbox -84.32 33.84 -75.46 36.59 --out /tmp/manifest.json

    # Just show what it would do, no API call:
    python -m leo_pipeline.run_discovery --bbox -84.32 33.84 -75.46 36.59 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
from pathlib import Path

import leo_pipeline.tools as tools
from leo_pipeline.agents import DATA_DISCOVERY_AGENT
from leo_pipeline.config import MODELS, PATHS, require_api_key

FACTORS = ("terrain", "surface", "canopy", "buildings")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leo-discovery",
        description="Run the A1 data-discovery agent and write a ranked data manifest.",
    )
    p.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help="AOI bounding box (EPSG:4326); overrides the locations CSV.",
    )
    p.add_argument(
        "--out",
        help="Path to write the manifest JSON (default: data/interim/data_manifest.json).",
    )
    p.add_argument(
        "--allow-fallback",
        action="store_true",
        help="Permit the CONUS fallback AOI when there is no CSV and no --bbox.",
    )
    p.add_argument(
        "--model",
        default=MODELS.driver,
        help="Driver model that orchestrates the agent (default: %(default)s).",
    )
    p.add_argument(
        "--quiet", action="store_true", help="Suppress the streaming agent output."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and print the config, then exit without calling the API.",
    )
    return p


def _resolve_aoi(args: argparse.Namespace) -> str:
    """Decide the AOI and set the process-level override accordingly. Returns a label."""
    if args.bbox:
        tools.AOI_OVERRIDE = list(args.bbox)
        return f"--bbox override {args.bbox}"
    tools.AOI_OVERRIDE = None
    if PATHS.locations_csv.exists():
        return f"locations CSV ({PATHS.locations_csv.name})"
    if args.allow_fallback:
        return "CONUS fallback (no CSV; --allow-fallback)"
    raise SystemExit(
        f"No AOI available: locations CSV not found at {PATHS.locations_csv} and no "
        "--bbox given.\nPass --bbox MIN_LON MIN_LAT MAX_LON MAX_LAT, or --allow-fallback "
        "to use the CONUS bbox."
    )


def _resolve_out(args: argparse.Namespace) -> Path:
    """Redirect the manifest output path if --out is given. Returns the effective path."""
    if args.out:
        out = Path(args.out).expanduser().resolve()
        tools.DISCOVERY = dataclasses.replace(tools.DISCOVERY, manifest_path=out)
    return tools.DISCOVERY.manifest_path


def prepare(args: argparse.Namespace) -> dict[str, object]:
    """Validate inputs and apply the AOI / output overrides (no API calls)."""
    if args.bbox:
        lon0, lat0, lon1, lat1 = args.bbox
        if not (lon0 < lon1 and lat0 < lat1):
            raise SystemExit("--bbox must be MIN_LON MIN_LAT MAX_LON MAX_LAT with min < max")
        if not (-180 <= lon0 <= 180 and -180 <= lon1 <= 180):
            raise SystemExit("--bbox longitudes must be within -180..180")
        if not (-90 <= lat0 <= 90 and -90 <= lat1 <= 90):
            raise SystemExit("--bbox latitudes must be within -90..90")
    aoi_source = _resolve_aoi(args)
    out_path = _resolve_out(args)
    return {"aoi_source": aoi_source, "out_path": out_path}


def build_discovery_options(model: str):
    """A focused ClaudeAgentOptions registering only the data-discovery agent."""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        model=model,
        mcp_servers={"leo": tools.LEO_TOOLS_SERVER},
        allowed_tools=tools.TOOL_NAMES + ["WebSearch", "WebFetch"],
        agents={"data-discovery": DATA_DISCOVERY_AGENT},
        cwd=str(PATHS.root),
        system_prompt=(
            "Delegate to the data-discovery subagent to search STAC + the web for the "
            "obstruction datasets covering the AOI and write the ranked data manifest, "
            "then stop."
        ),
    )


def _render(message) -> None:
    """Best-effort progress view: print assistant text and tool-use names."""
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return
    for block in content:
        name = getattr(block, "name", None)
        text = getattr(block, "text", None)
        if name:  # ToolUseBlock
            print(f"  -> tool: {name}")
        elif text:  # TextBlock
            print(text)


async def _run(options, verbose: bool) -> None:
    from claude_agent_sdk import ClaudeSDKClient

    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Use the data-discovery agent to search for the obstruction datasets and "
            "write the ranked data manifest for the AOI."
        )
        async for message in client.receive_response():
            if verbose:
                _render(message)


def _summarize_manifest(path: Path) -> None:
    if not path.exists():
        print(f"\n! No manifest written at {path} — the agent may not have completed.")
        return
    data = json.loads(path.read_text())
    entries = data.get("entries", [])
    selected = [e for e in entries if e.get("selected")]
    factors = sorted({e["factor"] for e in selected})
    missing = [f for f in FACTORS if f not in factors]

    print(f"\nManifest written: {path}")
    print(f"  AOI bbox       : {data.get('aoi_bbox')}")
    print(f"  entries        : {len(entries)} ({len(selected)} selected)")
    print(f"  factors covered: {factors or 'none'}")
    if missing:
        print(f"  factors MISSING: {missing}")
    for e in selected:
        rationale = e.get("rationale", "")
        print(f"   - {e['factor']:9} {str(e.get('dataset_id')):22} [{e.get('access')}] {rationale}")
    for note in data.get("notes") or []:
        print(f"  note (H1): {note}")


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    info = prepare(args)

    print(f"AOI source : {info['aoi_source']}")
    print(f"Manifest   : {info['out_path']}")
    print(f"Model      : {args.model}")
    if args.dry_run:
        print("(dry run — not calling the API)")
        return

    require_api_key()
    options = build_discovery_options(args.model)
    asyncio.run(_run(options, verbose=not args.quiet))
    _summarize_manifest(info["out_path"])  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
