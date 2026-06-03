"""Opt-in end-to-end run of the DATA_DISCOVERY_AGENT through the Claude Agent SDK.

This is the only test that spawns the Claude CLI, makes real API + STAC calls, and costs
tokens, so it is gated behind both the ``LEO_RUN_LIVE=1`` env switch and a present
ANTHROPIC_API_KEY, and tagged ``@pytest.mark.live`` so it never runs in the default suite.

Run it with:  LEO_RUN_LIVE=1 ../../.venv/bin/python -m pytest -m live -q
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

RUN_LIVE = os.getenv("LEO_RUN_LIVE") == "1" and bool(os.getenv("ANTHROPIC_API_KEY"))

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not RUN_LIVE,
        reason="set LEO_RUN_LIVE=1 and ANTHROPIC_API_KEY to run the live agent",
    ),
]


async def _drive_discovery_agent() -> None:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    from leo_pipeline.agents import DATA_DISCOVERY_AGENT
    from leo_pipeline.config import MODELS, PATHS
    from leo_pipeline.tools import LEO_TOOLS_SERVER, TOOL_NAMES

    options = ClaudeAgentOptions(
        model=MODELS.driver,
        mcp_servers={"leo": LEO_TOOLS_SERVER},
        allowed_tools=TOOL_NAMES + ["WebSearch", "WebFetch"],
        agents={"data-discovery": DATA_DISCOVERY_AGENT},
        cwd=str(PATHS.root),
        system_prompt=(
            "Delegate to the data-discovery subagent to produce the ranked obstruction "
            "data manifest for the input locations, then stop."
        ),
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Use the data-discovery agent to search for the obstruction datasets and "
            "write the ranked data manifest."
        )
        async for _ in client.receive_response():
            pass


def test_live_agent_writes_valid_manifest(redirect_manifest):
    # The SDK MCP server runs in-process, so the redirected manifest path applies even
    # for a live run -- the real data/interim/data_manifest.json is left untouched.
    asyncio.run(asyncio.wait_for(_drive_discovery_agent(), timeout=600))

    assert redirect_manifest.exists(), "agent did not write a manifest"
    raw = json.loads(redirect_manifest.read_text())

    assert raw["aoi_bbox"], "manifest missing AOI bbox"
    entries = raw["entries"]
    assert entries, "manifest has no dataset entries"
    assert any(e.get("selected") for e in entries), "no dataset was selected"

    factors = {e["factor"] for e in entries}
    assert {"terrain", "surface"} <= factors, f"core factors missing, got {factors}"
