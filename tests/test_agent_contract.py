"""Contract tests for the DATA_DISCOVERY_AGENT definition and its wiring.

These guard the agent's least-privilege tool allow-list, the prompt invariants the
methodology depends on, and the orchestrator wiring -- all without an API call.
"""

from __future__ import annotations

from claude_agent_sdk import SdkMcpTool

import leo_pipeline.tools as tools
from leo_pipeline import orchestrator
from leo_pipeline.agents import DATA_DISCOVERY_AGENT, all_agents
from leo_pipeline.tools import TOOL_NAMES

EXPECTED_TOOLS = [
    "mcp__leo__get_aoi_bbox",
    "mcp__leo__stac_search",
    "mcp__leo__opendata_registry_search",
    "mcp__leo__write_data_manifest",
    "WebSearch",
    "WebFetch",
]


def test_agent_tool_allow_list_is_exact():
    assert DATA_DISCOVERY_AGENT.tools == EXPECTED_TOOLS
    assert DATA_DISCOVERY_AGENT.model == "inherit"
    assert DATA_DISCOVERY_AGENT.description


def test_agent_prompt_states_workflow_invariants():
    prompt = DATA_DISCOVERY_AGENT.prompt.lower()
    # the tools it must drive, in its own words
    for tool in (
        "get_aoi_bbox",
        "stac_search",
        "opendata_registry_search",
        "write_data_manifest",
    ):
        assert tool in prompt
    # the H1 human gate and its single artifact
    assert "h1" in prompt
    assert "manifest" in prompt
    # all four obstruction factors the fused surface needs
    for factor in ("terrain", "surface", "canopy", "buildings"):
        assert factor in prompt


def test_agent_registered_under_stable_key():
    agents = all_agents()
    assert agents["data-discovery"] is DATA_DISCOVERY_AGENT


def test_no_dangling_mcp_tools():
    """Every mcp__leo__* tool the agent lists must actually be served."""
    served = {
        f"mcp__leo__{obj.name}"
        for obj in vars(tools).values()
        if isinstance(obj, SdkMcpTool)
    }
    mcp_tools = [t for t in DATA_DISCOVERY_AGENT.tools if t.startswith("mcp__leo__")]
    assert mcp_tools, "agent should reference at least one leo tool"
    for tool in mcp_tools:
        assert tool in TOOL_NAMES, f"{tool} missing from TOOL_NAMES"
        assert tool in served, f"{tool} not served by LEO_TOOLS_SERVER"


def test_orchestrator_wires_discovery_agent_and_tools():
    opts = orchestrator.build_options()  # no API call
    assert "data-discovery" in (opts.agents or {})
    for tool in DATA_DISCOVERY_AGENT.tools:
        assert tool in opts.allowed_tools, f"{tool} not allowed by orchestrator"
