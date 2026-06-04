"""Contract tests for the A3 GEO_ANALYSIS_AGENT and the run_analysis CLI plumbing.

Guards the agent's least-privilege tool allow-list, the prompt invariants the methodology
depends on, the orchestrator wiring, and the standalone CLI's arg/plan/compute resolution —
all without an API call or network.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from claude_agent_sdk import SdkMcpTool

import leo_pipeline.run_analysis as ra
import leo_pipeline.tools as tools
from leo_pipeline import orchestrator
from leo_pipeline.agents import GEO_ANALYSIS_AGENT, all_agents
from leo_pipeline.tools import TOOL_NAMES

from test_analysis_tools import _center_lonlat, _flat, write_surface

EXPECTED_TOOLS = [
    "mcp__leo__compute_sky_obstruction",
    "mcp__leo__find_clear_sky_spot",
]


# --- agent contract ------------------------------------------------------------------


def test_agent_tool_allow_list_is_exact():
    assert GEO_ANALYSIS_AGENT.tools == EXPECTED_TOOLS
    assert GEO_ANALYSIS_AGENT.model == "inherit"
    assert GEO_ANALYSIS_AGENT.description


def test_agent_prompt_states_workflow_invariants():
    prompt = GEO_ANALYSIS_AGENT.prompt.lower()
    for tool in ("compute_sky_obstruction", "find_clear_sky_spot"):
        assert tool in prompt
    # the three-state output + the tiers
    for tier in ("clear", "at_risk", "severe", "undetermined"):
        assert tier in prompt
    # the methodology primitive and the boundary (A3 reasons, doesn't fetch/browse)
    assert "horizon" in prompt
    assert "fetch" in prompt
    assert "web" in prompt
    # batch-per-tile keeps LLM cost O(tiles)
    assert "tile" in prompt


def test_agent_registered_under_stable_key():
    assert all_agents()["geo-analysis"] is GEO_ANALYSIS_AGENT


def test_no_dangling_mcp_tools():
    served = {
        f"mcp__leo__{obj.name}"
        for obj in vars(tools).values()
        if isinstance(obj, SdkMcpTool)
    }
    mcp_tools = [t for t in GEO_ANALYSIS_AGENT.tools if t.startswith("mcp__leo__")]
    assert mcp_tools
    for tool in mcp_tools:
        assert tool in TOOL_NAMES, f"{tool} missing from TOOL_NAMES"
        assert tool in served, f"{tool} not served by LEO_TOOLS_SERVER"


def test_old_stub_tools_are_gone():
    """The placeholder lookup_obstruction_layer / compute_risk_score stubs were replaced by
    the architecture's real A3 tools — they must no longer be served or named."""
    served = {f"mcp__leo__{o.name}" for o in vars(tools).values() if isinstance(o, SdkMcpTool)}
    for gone in ("mcp__leo__lookup_obstruction_layer", "mcp__leo__compute_risk_score"):
        assert gone not in served
        assert gone not in TOOL_NAMES


def test_orchestrator_wires_analysis_agent_and_tools():
    opts = orchestrator.build_options()  # no API call
    assert "geo-analysis" in (opts.agents or {})
    for tool in GEO_ANALYSIS_AGENT.tools:
        assert tool in opts.allowed_tools, f"{tool} not allowed by orchestrator"


# --- run_analysis CLI plumbing -------------------------------------------------------


def parse(argv):
    return ra.build_parser().parse_args(argv)


def test_resolve_work_point_requires_dsm_uri():
    with pytest.raises(SystemExit):
        ra.resolve_work(parse(["--point", "35.5", "-80.5"]))


def test_resolve_work_point_ad_hoc(tmp_path):
    path = write_surface(tmp_path / "s.tif", _flat())
    lon, lat = _center_lonlat(path)
    work = ra.resolve_work(parse(["--point", str(lat), str(lon), "--dsm-uri", str(path)]))
    assert len(work) == 1
    assert work[0]["tile_id"] == "adhoc_point"
    assert work[0]["points"][0]["lat"] == pytest.approx(lat)


def test_resolve_work_missing_cache_errors(tmp_path):
    # cache-dir override at an empty dir -> no surfaces -> clear error
    with pytest.raises(SystemExit):
        ra.prepare(parse(["--cache-dir", str(tmp_path / "empty"), "--limit", "1"]))


def test_compute_path_writes_findings_offline(tmp_path, monkeypatch):
    """The --compute path scores a point deterministically (no API) and writes findings JSON."""
    import leo_pipeline.config as cfg

    path = write_surface(tmp_path / "s.tif", _flat())
    lon, lat = _center_lonlat(path)
    findings_dir = tmp_path / "analysis"
    monkeypatch.setattr(ra, "ANALYSIS", cfg.Analysis(findings_dir=findings_dir))

    info = ra.prepare(parse(["--point", str(lat), str(lon), "--dsm-uri", str(path), "--compute"]))
    findings = ra.compute_findings(info)
    assert len(findings) == 1
    assert findings[0]["risk_tier"] == "clear"
    assert findings[0]["spec_version"]
    written = json.loads((findings_dir / "findings_adhoc_point.json").read_text())
    assert written[0]["location_id"] == "adhoc_point"


def test_tile_id_recovered_from_surface_uri():
    assert ra._tile_id_from_uri("/c/32617_158_806__c84a0b92_dsm.tif") == "32617_158_806"
    # ad-hoc surface with no "__" separator falls back to the file stem
    assert ra._tile_id_from_uri("/tmp/s.tif") == "s"


def test_findings_from_payloads_merges_subcalls_and_keeps_baseline_height():
    """Agentic capture: a tile split across sub-calls is unioned by location_id, and a
    raised-mount re-score must NOT overwrite the as-installed (lowest dish-height) verdict."""
    dsm = "/cache/32617_158_806__abc_dsm.tif"
    payloads = [
        # sub-call 1 of the 2.0 m first pass
        {"dsm_uri": dsm, "spec_version": "spec-x", "results": [
            {"location_id": "coord_1", "obstruction_pct": 0.0, "risk_tier": "clear",
             "confidence": "high", "dish_height_m": 2.0, "blocked_azimuths": [],
             "surface_provenance": "lidar"},
            {"location_id": "coord_2", "obstruction_pct": 12.0, "risk_tier": "at_risk",
             "confidence": "high", "dish_height_m": 2.0, "blocked_azimuths": [10],
             "surface_provenance": "lidar"},
        ]},
        # sub-call 2 of the same first pass (different points, same tile)
        {"dsm_uri": dsm, "spec_version": "spec-x", "results": [
            {"location_id": "coord_3", "obstruction_pct": 0.5, "risk_tier": "clear",
             "confidence": "medium", "dish_height_m": 2.0, "blocked_azimuths": [],
             "surface_provenance": "dem_fill"},
        ]},
        # raised-height re-score of the flagged point — advisory, must not clobber baseline
        {"dsm_uri": dsm, "spec_version": "spec-x", "results": [
            {"location_id": "coord_2", "obstruction_pct": 0.0, "risk_tier": "clear",
             "confidence": "high", "dish_height_m": 4.0, "blocked_azimuths": [],
             "surface_provenance": "lidar"},
        ]},
    ]
    rows = {r["location_id"]: r for r in ra.findings_from_payloads(payloads)}
    assert set(rows) == {"coord_1", "coord_2", "coord_3"}
    # coord_2 keeps the 2.0 m at_risk baseline, not the 4.0 m clear re-score
    assert rows["coord_2"]["dish_height_m"] == 2.0
    assert rows["coord_2"]["risk_tier"] == "at_risk"
    assert all(r["tile_id"] == "32617_158_806" for r in rows.values())


def test_write_findings_groups_by_tile(tmp_path, monkeypatch):
    import leo_pipeline.config as cfg

    findings_dir = tmp_path / "analysis"
    monkeypatch.setattr(ra, "ANALYSIS", cfg.Analysis(findings_dir=findings_dir))
    rows = [
        {"location_id": "coord_1", "tile_id": "T_A", "risk_tier": "clear"},
        {"location_id": "coord_2", "tile_id": "T_A", "risk_tier": "at_risk"},
        {"location_id": "coord_9", "tile_id": "T_B", "risk_tier": "clear"},
    ]
    by_tile = ra.write_findings(rows)
    assert set(by_tile) == {"T_A", "T_B"}
    assert len(json.loads((findings_dir / "findings_T_A.json").read_text())) == 2
    assert len(json.loads((findings_dir / "findings_T_B.json").read_text())) == 1


def test_theta_override_threads_into_sky_spec(tmp_path):
    path = write_surface(tmp_path / "s.tif", _flat())
    lon, lat = _center_lonlat(path)
    info = ra.prepare(
        parse(["--point", str(lat), str(lon), "--dsm-uri", str(path), "--theta", "10"])
    )
    assert info["sky_spec"]["min_elev_deg"] == 10.0


def test_dry_run_main_does_not_call_api(tmp_path, capsys):
    path = write_surface(tmp_path / "s.tif", _flat())
    lon, lat = _center_lonlat(path)
    ra.main(["--point", str(lat), str(lon), "--dsm-uri", str(path), "--dry-run"])
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "Points     : 1" in out
