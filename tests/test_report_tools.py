"""Deterministic tests for the A5 reporting tools + agent wiring (no network, no API key).

``aggregate_findings`` / ``render_map`` run over synthetic findings; ``write_report`` writes
into a redirected outputs dir. The agent-contract block guards the A5 least-privilege
allow-list and its orchestrator wiring, mirroring test_qa_tools.py for A4.
"""

from __future__ import annotations

import dataclasses

import pytest
from claude_agent_sdk import SdkMcpTool

import leo_pipeline.tools as tools
from leo_pipeline import orchestrator
from leo_pipeline.agents import REPORTING_AGENT, all_agents
from leo_pipeline.config import Reporting
from leo_pipeline.tools import TOOL_NAMES

from conftest import run_tool


@pytest.fixture
def redirect_outputs(tmp_path, monkeypatch):
    """Point the A5 outputs dir at tmp so the real outputs/ is never touched."""
    out = tmp_path / "outputs"
    monkeypatch.setattr(tools, "REPORTING", Reporting(outputs_dir=out))
    return out


def _f(i, tier, pct, *, lon=None, lat=None):
    # plain-ish coord ids; _county_weight_maps/_coord_lonlat_map need real coords on disk to
    # resolve, so for hermetic tool tests we pass lon/lat inline and expect county skipped.
    d = {"location_id": f"loc_{i}", "tile_id": "T", "obstruction_pct": pct,
         "risk_tier": tier, "confidence": "high", "spec_version": "spec-2026.06-theta25"}
    if lon is not None:
        d["lon"], d["lat"] = lon, lat
    return d


# --- aggregate_findings --------------------------------------------------------------


def test_aggregate_findings_tool_inline_totals_only():
    findings = [_f(i, "clear", 0.0) for i in range(5)] + [_f(5, "severe", 40.0)]
    env, payload = run_tool(tools.aggregate_findings, {"findings": findings})
    assert not env.get("is_error")
    assert payload["n_findings"] == 6
    assert payload["summary"]["tier_counts"]["severe"] == 1
    # plain ids -> no county map on disk -> run totals only + note
    assert payload["grouped_by"] == []
    assert "county" in " ".join(payload.get("notes", [])).lower()


def test_aggregate_findings_tool_errors_without_findings(tmp_path):
    env, text = run_tool(tools.aggregate_findings, {"findings_dir": str(tmp_path)})
    assert env.get("is_error")
    assert "no findings" in text


# --- render_map ----------------------------------------------------------------------


def test_render_map_tool_writes_geojson_and_html(redirect_outputs):
    findings = [_f(i, "severe", 40.0, lon=-80.0 + 0.001 * i, lat=35.0 + 0.001 * i) for i in range(4)]
    env, payload = run_tool(tools.render_map, {"findings": findings, "run_tiles": False})
    assert not env.get("is_error")
    assert payload["n_features"] == 4
    assert (redirect_outputs / "locations.geojson").exists()
    assert (redirect_outputs / "coverage_map.html").exists()


def test_render_map_tool_errors_without_findings(tmp_path):
    env, text = run_tool(tools.render_map, {"findings_dir": str(tmp_path)})
    assert env.get("is_error")
    assert "no findings" in text


# --- write_report --------------------------------------------------------------------


def test_write_report_persists_markdown(redirect_outputs):
    env, payload = run_tool(
        tools.write_report, {"content": "# Hello\n\nofficer report", "filename": "decision_log.md"}
    )
    assert not env.get("is_error")
    path = redirect_outputs / "decision_log.md"
    assert path.exists()
    assert path.read_text().startswith("# Hello")
    assert payload["bytes"] > 0


@pytest.mark.parametrize(
    "args, needle",
    [
        ({"content": ""}, "non-empty"),
        ({"content": "x", "filename": "../escape.md"}, "bare basename"),
        ({"content": "x", "filename": "evil.py"}, "must end with"),
    ],
)
def test_write_report_rejects_bad_input(redirect_outputs, args, needle):
    env, text = run_tool(tools.write_report, args)
    assert env.get("is_error")
    assert needle in text


# --- A5 agent contract ---------------------------------------------------------------

EXPECTED_A5_TOOLS = [
    "mcp__leo__aggregate_findings",
    "mcp__leo__render_map",
    "mcp__leo__write_report",
    "mcp__leo__query_locations",
]


def test_reporting_agent_allow_list_is_exact():
    assert REPORTING_AGENT.tools == EXPECTED_A5_TOOLS
    assert REPORTING_AGENT.model == "inherit"
    assert REPORTING_AGENT.description


def test_reporting_agent_prompt_states_invariants():
    prompt = REPORTING_AGENT.prompt.lower()
    for tool in ("aggregate_findings", "render_map", "write_report"):
        assert tool in prompt
    assert "h2" in prompt              # the insight sign-off gate it feeds
    assert "narrative" in prompt       # the LLM writes prose, tools compute numbers
    assert "caveat" in prompt          # must surface undetermined / not-a-guarantee


def test_reporting_agent_registered_and_tools_served():
    assert all_agents()["reporting"] is REPORTING_AGENT
    served = {
        f"mcp__leo__{obj.name}" for obj in vars(tools).values() if isinstance(obj, SdkMcpTool)
    }
    for tool in (t for t in REPORTING_AGENT.tools if t.startswith("mcp__leo__")):
        assert tool in TOOL_NAMES, f"{tool} missing from TOOL_NAMES"
        assert tool in served, f"{tool} not served by LEO_TOOLS_SERVER"


def test_orchestrator_wires_reporting_agent_and_tools():
    opts = orchestrator.build_options()  # no API call
    assert "reporting" in (opts.agents or {})
    for tool in REPORTING_AGENT.tools:
        assert tool in opts.allowed_tools, f"{tool} not allowed by orchestrator"
