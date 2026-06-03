"""Run the A4 validation/QA agent on its own, from one command.

Wraps ``QA_AGENT`` (A4) in a minimal Claude Agent SDK harness so a user can validate a run —
audit the input rows and scan the A3 obstruction findings for anomalies — without
hand-assembling options. It mirrors ``run_analysis``: read the per-tile findings A3 wrote,
run the deterministic input + output checks, and surface anything that should reach the H2
human gate. The agent triages/explains; the detection (``leo_pipeline.qa``) is deterministic.

Two paths:

- default (agentic): drive A4 — it calls qa_input_audit + qa_location_batch and reasons about
  each anomaly. Needs ANTHROPIC_API_KEY.
- ``--compute``: run the SAME deterministic QA engine directly (no agent, no API), writing a
  QA report JSON and printing the anomalies. The offline / CI / verification path.

Examples
--------
    # Validate every analysis finding on disk, with the agent:
    python -m leo_pipeline.run_qa

    # Deterministically QA one tile's findings offline and write the report:
    python -m leo_pipeline.run_qa --tile 32617_120_3940 --compute

    # Offline QA of a findings dir, input audit against a custom AOI:
    python -m leo_pipeline.run_qa --compute --aoi -84.32 33.84 -75.46 36.59

    # Show what it would do, no API call:
    python -m leo_pipeline.run_qa --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import leo_pipeline.tools as tools
from leo_pipeline import qa as qa_engine
from leo_pipeline.agents import QA_AGENT
from leo_pipeline.config import ANALYSIS, MODELS, PATHS, QA, require_api_key

# A4 reads only the QA tools + the read-only SQL surface — never STAC, never risk scoring,
# never write tools. WebFetch (a built-in) lets it sanity-check a flagged region.
A4_TOOL_NAMES = [
    "mcp__leo__qa_input_audit",
    "mcp__leo__qa_location_batch",
    "mcp__leo__query_locations",
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leo-qa",
        description="Run the A4 validation/QA agent: input-quality audit + output-anomaly scan.",
    )
    p.add_argument("--tile", help="QA a single tile_id's findings (default: all on disk).")
    p.add_argument(
        "--findings-dir",
        help=f"Directory of A3 findings_*.json (default: {ANALYSIS.findings_dir}).",
    )
    p.add_argument(
        "--aoi",
        nargs=4,
        type=float,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help="AOI bbox for the input audit (default: A1 manifest, else the QA spec default).",
    )
    p.add_argument(
        "--no-input",
        action="store_true",
        help="Skip the input-quality audit (output-anomaly scan only).",
    )
    p.add_argument(
        "--compute",
        action="store_true",
        help="Deterministic path: run the QA engine directly (no agent, no API) and write "
        "the report.",
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
        help="Resolve the findings + AOI and print the plan, then exit (no API call).",
    )
    return p


def prepare(args: argparse.Namespace) -> dict:
    """Resolve the findings dir, the AOI, and load the findings to QA (no API)."""
    findings_dir = (
        Path(args.findings_dir).expanduser().resolve()
        if args.findings_dir
        else ANALYSIS.findings_dir
    )
    tile = (args.tile or "").strip() or None
    findings, sources = tools._load_findings(None, findings_dir, tile)
    aoi = tools._resolve_aoi(args.aoi)
    return {
        "findings_dir": findings_dir,
        "tile": tile,
        "findings": findings,
        "sources": sources,
        "aoi": aoi,
        "check_input": not args.no_input,
    }


# --- deterministic path (--compute): run the QA engine directly ----------------------


def compute_report(info: dict) -> dict:
    """Run the deterministic input + output QA and write the report to QA.reports_dir.
    The no-API verification/CI path; the agentic default calls the same engine via the
    qa_input_audit / qa_location_batch tools."""
    report: dict = {"qa_spec_version": QA.qa_spec_version}

    if info["check_input"]:
        try:
            con = tools.ingest.connect()
        except FileNotFoundError:
            report["input_quality"] = {"skipped": "locations CSV not on disk"}
        else:
            try:
                counts = qa_engine.input_quality_counts(con, info["aoi"])
            finally:
                con.close()
            iq = qa_engine.input_quality_report(counts, info["aoi"])
            iq["aoi_bbox"] = info["aoi"]
            report["input_quality"] = iq

    findings = info["findings"]
    if findings:
        county_of, weight_of = tools._county_weight_maps(findings)
        out = qa_engine.run_output_qa(
            findings, county_of=county_of or None, weight_of=weight_of or None
        )
        out["n_findings"] = len(findings)
        out["sources"] = info["sources"]
        if not county_of:
            out.setdefault("notes", []).append(
                "county grouping skipped (dedup maps / CSV not on disk); tile grouping only"
            )
        report["output_qa"] = out
    else:
        report["output_qa"] = {"skipped": f"no findings in {info['findings_dir']}"}

    out_dir = QA.reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"qa_report_{info['tile']}.json" if info["tile"] else "qa_report.json"
    out_path = out_dir / name
    out_path.write_text(json.dumps(report, indent=2, default=str))
    report["_path"] = str(out_path)
    return report


def _print_report(report: dict) -> None:
    iq = report.get("input_quality")
    if iq and "skipped" not in iq:
        print(
            f"\nInput audit : {iq['quarantine_total']:,} / {iq['total_rows']:,} rows quarantined "
            f"({iq['quarantine_rate']:.4%})"
        )
        for issue in iq.get("issues", []):
            print(f"  {issue['kind']:16} {issue['count']:>10,}  {issue['detail']}")
    elif iq:
        print(f"\nInput audit : skipped ({iq['skipped']})")

    out = report.get("output_qa", {})
    if "skipped" in out:
        print(f"Output QA   : skipped ({out['skipped']})")
        return
    summ = out["summary"]
    print(
        f"\nOutput QA   : {out['n_findings']:,} findings  "
        f"(grouped by {', '.join(out['grouped_by'])})"
    )
    print(f"  tiers       {summ['tier_counts']}")
    print(f"  at_risk_rate {summ['at_risk_rate']:.2%}  undetermined {summ['undetermined_rate']:.2%}")
    anomalies = out["anomalies"]
    if not anomalies:
        print("  anomalies   none — PASS")
        return
    print(f"  anomalies   {len(anomalies)} -> REVIEW"
          + ("  (has CRITICAL)" if out.get("has_critical") else ""))
    for a in anomalies:
        line = f"    [{a['severity']:8}] {a['rule']:24} {a['scope']:18} {a['detail']}"
        print(line)
    print(f"\n-> {report.get('_path')}")


# --- agentic path --------------------------------------------------------------------


def build_qa_options(model: str):
    """A focused ClaudeAgentOptions registering only the A4 QA agent."""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        model=model,
        mcp_servers={"leo": tools.LEO_TOOLS_SERVER},
        allowed_tools=A4_TOOL_NAMES + ["WebFetch"],
        agents={"qa": QA_AGENT},
        cwd=str(PATHS.root),
        system_prompt=(
            "Delegate to the qa subagent to validate this run: audit the input rows "
            "(qa_input_audit) and scan the A3 findings for output anomalies "
            "(qa_location_batch), triage anything flagged, and return a PASS/REVIEW verdict "
            "for the H2 human gate. Do not mutate state or publish results; then stop."
        ),
    )


def _build_query(info: dict) -> str:
    """The task message: which findings to QA, the AOI, and whether to audit the input."""
    lines = [
        "Validate this pipeline run before it reaches a human.",
        "",
    ]
    if info["check_input"]:
        lines.append(
            f"1. Audit the input rows with qa_input_audit (AOI {info['aoi']}); call out only "
            "an implausibly large quarantine bucket."
        )
    lines.append(
        f"{'2' if info['check_input'] else '1'}. Scan the A3 findings with qa_location_batch"
        + (f" for tile {info['tile']}." if info["tile"] else " (all tiles on disk).")
    )
    lines.append(
        "Triage every anomaly and return a PASS or REVIEW verdict with specific reasons and "
        "the qa_spec_version."
    )
    n = len(info["findings"])
    lines.append("")
    lines.append(
        f"({n} finding(s) are on disk in {info['findings_dir']}; "
        "let qa_location_batch load them.)"
    )
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
    print(f"Input audit  : {'on' if info['check_input'] else 'off'} (AOI {info['aoi']})")
    print(f"QA spec      : {QA.qa_spec_version}")
    if args.dry_run:
        print("(dry run — not calling the API)")
        return

    if args.compute:
        report = compute_report(info)
        _print_report(report)
        return

    print(f"Model        : {args.model}")
    require_api_key()
    options = build_qa_options(args.model)
    query = _build_query(info)
    asyncio.run(_run(options, query, verbose=not args.quiet))


if __name__ == "__main__":
    main()
