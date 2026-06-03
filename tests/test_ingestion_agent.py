"""Contract tests for the A2 INGESTION_AGENT and the run_ingestion CLI plumbing.

Guards the agent's least-privilege tool allow-list, the prompt invariants the methodology
depends on, the orchestrator wiring, and the standalone CLI's arg/plan resolution — all
without an API call or network.
"""

from __future__ import annotations

import json

import pytest
from claude_agent_sdk import SdkMcpTool

import leo_pipeline.run_ingestion as ri
import leo_pipeline.tools as tools
from leo_pipeline import orchestrator
from leo_pipeline.agents import INGESTION_AGENT, all_agents
from leo_pipeline.tools import TOOL_NAMES

EXPECTED_TOOLS = [
    "mcp__leo__cache_rw",
    "mcp__leo__stac_item_read",
    "mcp__leo__fetch_aligned_surface",
]


# --- agent contract ------------------------------------------------------------------


def test_agent_tool_allow_list_is_exact():
    assert INGESTION_AGENT.tools == EXPECTED_TOOLS
    assert INGESTION_AGENT.model == "inherit"
    assert INGESTION_AGENT.description


def test_agent_prompt_states_workflow_invariants():
    prompt = INGESTION_AGENT.prompt.lower()
    for tool in ("cache_rw", "stac_item_read", "fetch_aligned_surface"):
        assert tool in prompt
    # the full fallback hierarchy in its own words
    for mode in ("true_dsm", "pseudo_dsm", "cover_proxy"):
        assert mode in prompt
    assert "tile" in prompt
    # the boundary: A2 does NOT score risk and does NOT browse the web
    assert "risk" in prompt
    assert "web" in prompt


def test_agent_registered_under_stable_key():
    assert all_agents()["ingestion"] is INGESTION_AGENT


def test_no_dangling_mcp_tools():
    served = {
        f"mcp__leo__{obj.name}"
        for obj in vars(tools).values()
        if isinstance(obj, SdkMcpTool)
    }
    mcp_tools = [t for t in INGESTION_AGENT.tools if t.startswith("mcp__leo__")]
    assert mcp_tools
    for tool in mcp_tools:
        assert tool in TOOL_NAMES, f"{tool} missing from TOOL_NAMES"
        assert tool in served, f"{tool} not served by LEO_TOOLS_SERVER"


def test_orchestrator_wires_ingestion_agent_and_tools():
    opts = orchestrator.build_options()  # no API call
    assert "ingestion" in (opts.agents or {})
    for tool in INGESTION_AGENT.tools:
        assert tool in opts.allowed_tools, f"{tool} not allowed by orchestrator"


def test_old_inputload_dedup_tool_stays_served_but_unattached():
    """De-dup duty dropped from the agent layer, but the tool stays served + runnable."""
    served = {f"mcp__leo__{o.name}" for o in vars(tools).values() if isinstance(o, SdkMcpTool)}
    assert "mcp__leo__deduplicate_coordinates" in served
    assert "mcp__leo__deduplicate_coordinates" not in INGESTION_AGENT.tools


# --- run_ingestion CLI plumbing ------------------------------------------------------


def parse(argv):
    return ri.build_parser().parse_args(argv)


def test_load_manifest_summarises_selected_factors(tmp_path):
    manifest = {
        "aoi_bbox": [-84, 33, -75, 36],
        "entries": [
            {"factor": "terrain", "dataset_id": "cop-dem-glo-30", "access": "stac", "selected": True},
            {"factor": "canopy", "dataset_id": "meta", "access": "web", "selected": True,
             "source_url": "s3://x"},
            {"factor": "surface", "dataset_id": "3dep", "access": "stac", "selected": False},
        ],
    }
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest))

    out = ri.load_manifest(path)
    assert out["aoi_bbox"] == [-84, 33, -75, 36]
    assert set(out["factors"]) == {"terrain", "canopy"}  # only selected
    assert out["factors"]["canopy"]["source_url"] == "s3://x"
    assert out["factors"]["terrain"]["collection"] == "cop-dem-glo-30"


def test_resolve_tiles_adhoc_bbox():
    tiles = ri.resolve_tiles(parse(["--bbox", "-80.0", "35.0", "-79.95", "35.05"]))
    assert len(tiles) == 1
    assert tiles[0]["bbox"] == [-80.0, 35.0, -79.95, 35.05]
    assert tiles[0]["tile_id"].startswith("adhoc_")


def test_resolve_tiles_bbox_min_max_validated():
    with pytest.raises(SystemExit):
        ri.resolve_tiles(parse(["--bbox", "-79", "36", "-80", "35"]))


def test_prepare_missing_manifest_errors(tmp_path):
    with pytest.raises(SystemExit):
        ri.prepare(parse(["--manifest", str(tmp_path / "nope.json"), "--bbox", "-80", "35", "-79", "36"]))


def test_dry_run_main_does_not_call_api(tmp_path, capsys):
    manifest = {
        "aoi_bbox": [-84, 33, -75, 36],
        "entries": [{"factor": "terrain", "dataset_id": "cop-dem-glo-30", "access": "stac", "selected": True}],
    }
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest))

    ri.main(["--manifest", str(path), "--bbox", "-80", "35", "-79.95", "35.05", "--dry-run"])
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "terrain" in out
