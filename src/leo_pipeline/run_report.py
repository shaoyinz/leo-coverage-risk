"""Run the A5 reporting/insight agent on its own, from one command.

Wraps ``REPORTING_AGENT`` (A5) in a minimal Claude Agent SDK harness so a user can turn the
A3 obstruction findings (and the A4 anomalies) into the three officer-facing deliverables —
county/state aggregates, the interactive PMTiles+MapLibre map, and the plain-English decision
log — without hand-assembling options. It mirrors ``run_qa``: read the per-tile findings A3
wrote, build the artifacts, and write them to ``outputs/`` for the H2 sign-off.

Two paths:

- default (agentic): drive A5 — it calls aggregate_findings + render_map and composes the
  officer narrative via write_report. Needs ANTHROPIC_API_KEY.
- ``--compute``: run the SAME deterministic report engine directly (no agent, no API),
  writing the aggregates JSON, the map (GeoJSON + PMTiles + HTML), and the templated decision
  log. The offline / CI / verification path.

Examples
--------
    # Build the full report + interactive map for every finding on disk, with the agent:
    python -m leo_pipeline.run_report

    # Deterministically build the report for one tile offline (no API):
    python -m leo_pipeline.run_report --tile 32617_103_779 --compute

    # Offline, skip the tippecanoe tiling step (GeoJSON + HTML only):
    python -m leo_pipeline.run_report --compute --no-tiles

    # Show what it would do, no API call:
    python -m leo_pipeline.run_report --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import leo_pipeline.tools as tools
from leo_pipeline import report as report_engine
from leo_pipeline.agents import REPORTING_AGENT
from leo_pipeline.config import ANALYSIS, MODELS, PATHS, QA, REPORTING, require_api_key

# A5 reads only the reporting tools + the read-only SQL surface — never STAC, never risk
# scoring, never the open web. write_report is a scoped write into outputs/ only.
A5_TOOL_NAMES = [
    "mcp__leo__aggregate_findings",
    "mcp__leo__render_map",
    "mcp__leo__write_report",
    "mcp__leo__query_locations",
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leo-report",
        description="Run the A5 reporting agent: county/state aggregates + interactive map + decision log.",
    )
    p.add_argument("--tile", help="Report on a single tile_id's findings (default: all on disk).")
    p.add_argument(
        "--findings-dir",
        help=f"Directory of A3 findings_*.json (default: {ANALYSIS.findings_dir}).",
    )
    p.add_argument(
        "--outputs-dir",
        help=f"Where to write the report artifacts (default: {REPORTING.outputs_dir}).",
    )
    p.add_argument(
        "--no-tiles",
        action="store_true",
        help="Skip the tippecanoe PMTiles step (still writes the GeoJSON + MapLibre HTML).",
    )
    p.add_argument(
        "--compute",
        action="store_true",
        help="Deterministic path: run the report engine directly (no agent, no API) and write "
        "all artifacts.",
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
        help="Resolve the findings + outputs dir and print the plan, then exit (no API call).",
    )
    return p


def _load_anomalies() -> list[dict]:
    """Best-effort A4 anomalies from the QA report, to fold into the decision log's H2 status."""
    try:
        report = json.loads((QA.reports_dir / "qa_report.json").read_text())
        return report.get("output_qa", {}).get("anomalies", []) or []
    except Exception:
        return []


def prepare(args: argparse.Namespace) -> dict:
    """Resolve the findings, the join maps, the anomalies, and the outputs dir (no API)."""
    findings_dir = (
        Path(args.findings_dir).expanduser().resolve()
        if args.findings_dir
        else ANALYSIS.findings_dir
    )
    tile = (args.tile or "").strip() or None
    findings, sources = tools._load_findings(None, findings_dir, tile)
    out_dir = (
        Path(args.outputs_dir).expanduser().resolve()
        if args.outputs_dir
        else REPORTING.outputs_dir
    )
    county_of, weight_of = tools._county_weight_maps(findings) if findings else ({}, {})
    lonlat_of = tools._coord_lonlat_map(findings) if findings else {}
    return {
        "findings_dir": findings_dir,
        "tile": tile,
        "findings": findings,
        "sources": sources,
        "out_dir": out_dir,
        "county_of": county_of,
        "weight_of": weight_of,
        "lonlat_of": lonlat_of,
        "anomalies": _load_anomalies(),
        "run_tiles": not args.no_tiles,
    }


# --- deterministic path (--compute): run the report engine directly ------------------


def compute_report(info: dict) -> dict:
    """Build all three A5 deliverables deterministically and write them to the outputs dir.
    The no-API verification/CI path; the agentic default calls the same engine via the
    aggregate_findings / render_map tools (+ write_report for the narrative)."""
    result = report_engine.build_report(
        info["findings"],
        info["out_dir"],
        county_of=info["county_of"] or None,
        weight_of=info["weight_of"] or None,
        lonlat_of=info["lonlat_of"] or None,
        anomalies=info["anomalies"] or None,
        run_tiles=info["run_tiles"],
    )
    return result


def _print_report(result: dict) -> None:
    agg = result["aggregates"]
    summ = agg["summary"]
    print(f"\nReport      : {summ['n_findings']:,} findings  "
          f"(grouped by {', '.join(agg['grouped_by']) or 'run totals only'})")
    print(f"  tiers       {summ['tier_counts']}")
    states = agg.get("states") or []
    for s in states[:5]:
        print(f"  {s.get('state_name', s['region']):16} {s['households']:>8,} hh  "
              f"{s['at_risk_households']:>7,} at-risk ({s['at_risk_rate']:.1%})")
    counties = agg.get("counties") or []
    if counties:
        print(f"  priority county: {counties[0]['region']} "
              f"({counties[0]['at_risk_households']:,} at-risk households)")
    m = result["map"]
    print(f"\nMap         : {m['n_features']:,} features — {m['note']}")
    print("\nArtifacts:")
    for a in result["artifacts"]:
        print(f"  {a['kind']:13} {a['path']}")


# --- agentic path --------------------------------------------------------------------


def build_report_options(model: str):
    """A focused ClaudeAgentOptions registering only the A5 reporting agent."""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        model=model,
        mcp_servers={"leo": tools.LEO_TOOLS_SERVER},
        allowed_tools=A5_TOOL_NAMES,
        agents={"reporting": REPORTING_AGENT},
        cwd=str(PATHS.root),
        system_prompt=(
            "Delegate to the reporting subagent to produce the officer-facing deliverables: "
            "call aggregate_findings for the county/state numbers, render_map for the "
            "interactive map, compose the plain-English decision log (headline, priority "
            "counties, caveats, QA/H2 status) and persist it with write_report. Cite only the "
            "tools' numbers; then stop."
        ),
    )


def _build_query(info: dict) -> str:
    n = len(info["findings"])
    lines = [
        "Produce the officer-facing report for this run.",
        "",
        f"1. aggregate_findings"
        + (f" for tile {info['tile']}." if info["tile"] else " (all tiles on disk)."),
        "2. render_map to build the interactive risk map in outputs/.",
        "3. Compose the plain-English decision log — headline tier breakdown, priority "
        "counties/states, and the caveats (undetermined/low-confidence share, 'verify on "
        "site, not a guarantee', the obstruction spec_version)"
        + (f", and the {len(info['anomalies'])} A4 anomaly(ies) and their H2 status"
           if info["anomalies"] else "")
        + " — then write_report it as decision_log.md.",
        "",
        f"({n} finding(s) are on disk in {info['findings_dir']}; let the tools load them.)",
    ]
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

    print(f"Findings dir : {info['findings_dir']}")
    print(f"Findings     : {len(info['findings'])}"
          + (f" (tile {info['tile']})" if info["tile"] else ""))
    print(f"Outputs dir  : {info['out_dir']}")
    print(f"Tiling       : {'on (tippecanoe)' if info['run_tiles'] else 'off (--no-tiles)'}"
          + ("" if report_engine.tippecanoe_available() else "  [tippecanoe NOT on PATH]"))
    print(f"A4 anomalies : {len(info['anomalies'])}")
    print(f"Report spec  : {REPORTING.report_version}")
    if args.dry_run:
        print("(dry run — not calling the API)")
        return

    if args.compute:
        if not info["findings"]:
            raise SystemExit(
                f"No findings in {info['findings_dir']}. Run the analysis first "
                "(python -m leo_pipeline.run_analysis --compute)."
            )
        result = compute_report(info)
        _print_report(result)
        return

    print(f"Model        : {args.model}")
    require_api_key()
    options = build_report_options(args.model)
    query = _build_query(info)
    asyncio.run(_run(options, query, verbose=not args.quiet))


if __name__ == "__main__":
    main()
