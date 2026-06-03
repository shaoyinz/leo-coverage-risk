"""Deterministic tests for the A4 QA tools + agent wiring (no network, no API key).

``qa_location_batch`` runs over synthetic findings (inline and loaded from a tmp dir);
``qa_input_audit`` runs over a tiny in-memory ``locations`` table swapped in for
``ingest.connect``. The agent-contract block guards the A4 least-privilege allow-list and
its orchestrator wiring, mirroring test_agent_contract.py for A1.
"""

from __future__ import annotations

import json

import duckdb
import pytest
from claude_agent_sdk import SdkMcpTool

import leo_pipeline.tools as tools
from leo_pipeline import orchestrator
from leo_pipeline.agents import QA_AGENT, all_agents
from leo_pipeline.tools import TOOL_NAMES

from conftest import run_tool

AOI = [-84.0, 33.0, -75.0, 36.0]


def _findings(n, *, tile, tier, pct, lid_prefix="loc"):
    # plain (non-coord_) ids so _county_weight_maps early-returns -> hermetic, tile-only.
    return [
        {"location_id": f"{lid_prefix}{i}", "tile_id": tile, "obstruction_pct": pct,
         "risk_tier": tier, "confidence": "high", "spec_version": "spec-2026.06-theta25"}
        for i in range(n)
    ]


# --- qa_location_batch ---------------------------------------------------------------


def test_qa_location_batch_inline_clean_passes():
    findings = _findings(40, tile="T1", tier="clear", pct=0.0)
    env, payload = run_tool(tools.qa_location_batch, {"findings": findings})
    assert not env.get("is_error")
    assert payload["review_required"] is False
    assert payload["n_findings"] == 40
    assert payload["grouped_by"] == ["tile"]
    assert payload["qa_spec_version"]


def test_qa_location_batch_flags_saturated_region():
    findings = _findings(40, tile="HOT", tier="severe", pct=40.0)
    env, payload = run_tool(tools.qa_location_batch, {"findings": findings})
    assert not env.get("is_error")
    assert payload["review_required"] is True
    rules = {a["rule"] for a in payload["anomalies"]}
    assert "saturated_region" in rules


def test_qa_location_batch_loads_from_dir(tmp_path):
    (tmp_path / "findings_T1.json").write_text(
        json.dumps(_findings(40, tile="T1", tier="severe", pct=40.0))
    )
    env, payload = run_tool(
        tools.qa_location_batch, {"findings_dir": str(tmp_path), "tile": "T1"}
    )
    assert not env.get("is_error")
    assert payload["n_findings"] == 40
    assert "county grouping skipped" in " ".join(payload.get("notes", []))


def test_qa_location_batch_errors_when_no_findings(tmp_path):
    env, text = run_tool(tools.qa_location_batch, {"findings_dir": str(tmp_path)})
    assert env.get("is_error")
    assert "no findings" in text


# --- qa_input_audit ------------------------------------------------------------------


@pytest.fixture
def stub_locations(monkeypatch):
    """Swap ``ingest.connect`` for a tiny in-memory ``locations`` table."""
    def _connect(csv_path=None):
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE locations (location_id VARCHAR, latitude DOUBLE, longitude DOUBLE, geoid_cb VARCHAR)"
        )
        con.executemany(
            "INSERT INTO locations VALUES (?, ?, ?, ?)",
            [
                ("good", 35.0, -80.0, "37019"),
                ("nullrow", None, -80.0, "x"),
                ("island", 0.0, 0.0, "x"),
                ("swap", -80.0, 35.0, "x"),
            ],
        )
        return con

    monkeypatch.setattr(tools.ingest, "connect", _connect)


def test_qa_input_audit_reports_buckets(stub_locations):
    env, payload = run_tool(tools.qa_input_audit, {"aoi_bbox": AOI})
    assert not env.get("is_error")
    assert payload["total_rows"] == 4
    assert payload["quarantine_total"] == 3  # null + null_island + off_aoi(=swap)
    assert payload["swapped_suspect"] == 1
    assert payload["aoi_bbox"] == AOI
    kinds = {i["kind"] for i in payload["issues"]}
    assert {"null_coord", "null_island", "off_aoi", "lat_lon_swapped"} <= kinds


def test_qa_input_audit_missing_csv_errors(monkeypatch):
    def _raise(csv_path=None):
        raise FileNotFoundError("no csv")

    monkeypatch.setattr(tools.ingest, "connect", _raise)
    env, text = run_tool(tools.qa_input_audit, {"aoi_bbox": AOI})
    assert env.get("is_error")
    assert "locations CSV not found" in text


# --- A4 agent contract ---------------------------------------------------------------

EXPECTED_QA_TOOLS = [
    "mcp__leo__qa_input_audit",
    "mcp__leo__qa_location_batch",
    "mcp__leo__query_locations",
    "WebFetch",
]


def test_qa_agent_allow_list_is_exact():
    assert QA_AGENT.tools == EXPECTED_QA_TOOLS
    assert QA_AGENT.model == "inherit"
    assert QA_AGENT.description


def test_qa_agent_prompt_states_invariants():
    prompt = QA_AGENT.prompt.lower()
    for tool in ("qa_input_audit", "qa_location_batch"):
        assert tool in prompt
    assert "h2" in prompt          # the human-review gate it feeds
    assert "read-only" in prompt   # never mutates state
    assert "triage" in prompt


def test_qa_agent_registered_and_tools_served():
    assert all_agents()["qa"] is QA_AGENT
    served = {
        f"mcp__leo__{obj.name}" for obj in vars(tools).values() if isinstance(obj, SdkMcpTool)
    }
    for tool in (t for t in QA_AGENT.tools if t.startswith("mcp__leo__")):
        assert tool in TOOL_NAMES, f"{tool} missing from TOOL_NAMES"
        assert tool in served, f"{tool} not served by LEO_TOOLS_SERVER"


def test_orchestrator_wires_qa_agent_and_tools():
    opts = orchestrator.build_options()  # no API call
    assert "qa" in (opts.agents or {})
    for tool in QA_AGENT.tools:
        assert tool in opts.allowed_tools, f"{tool} not allowed by orchestrator"
