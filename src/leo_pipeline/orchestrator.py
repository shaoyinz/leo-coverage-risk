"""Pipeline orchestrator: wires tools + agents into Claude Agent SDK options.

At this stage it only *assembles and reports* the configuration so the wiring can be
verified end-to-end (SDK imports + CLI spawn path) before any analysis logic exists.
Running it as a module prints the wired config. Driving a real session is gated behind
``--run`` and requires ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio

from claude_agent_sdk import ClaudeAgentOptions

from leo_pipeline.agents import all_agents
from leo_pipeline.config import MODELS, PATHS, require_api_key
from leo_pipeline.state import PipelineState
from leo_pipeline.tools import LEO_TOOLS_SERVER, TOOL_NAMES


def build_options() -> ClaudeAgentOptions:
    """Assemble the ClaudeAgentOptions for the multi-agent run."""
    return ClaudeAgentOptions(
        model=MODELS.driver,
        mcp_servers={"leo": LEO_TOOLS_SERVER},
        # The data-discovery agent also uses the built-in WebSearch/WebFetch tools to
        # source datasets that aren't in a STAC catalog (e.g. canopy height).
        allowed_tools=TOOL_NAMES + ["WebSearch", "WebFetch"],
        agents=all_agents(),
        cwd=str(PATHS.root),
        system_prompt=(
            "You orchestrate a LEO satellite coverage-risk pipeline. Delegate to the "
            "subagents in order, threading state between them: (1) ingestion profiles "
            "and de-duplicates the locations; (2) data-discovery searches STAC + the web "
            "for the obstruction datasets covering that area and writes a ranked data "
            "manifest — STOP and surface that manifest for human approval (the H1 gate) "
            "on cost/licensing before any bulk download; (3) geo-analysis samples the "
            "approved layers and scores risk; (4) qa validates the outputs. Surface "
            "anomalies for human review."
        ),
    )


def describe() -> str:
    """Human-readable summary of the wired configuration (no API calls)."""
    opts = build_options()
    state = PipelineState()
    lines = [
        "LEO coverage-risk pipeline — wired configuration",
        f"  driver model : {MODELS.driver}",
        f"  worker model : {MODELS.worker}",
        f"  repo root    : {PATHS.root}",
        f"  mcp servers  : {list(opts.mcp_servers)}",
        f"  tools        : {opts.allowed_tools}",
        f"  agents       : {list(opts.agents or {})}",
        f"  start stage  : {state.stage.value}",
    ]
    return "\n".join(lines)


async def run() -> None:
    """Drive a real multi-agent session. Requires ANTHROPIC_API_KEY."""
    require_api_key()
    from claude_agent_sdk import ClaudeSDKClient

    options = build_options()
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Begin: ask the ingestion agent to profile the provided locations dataset."
        )
        async for message in client.receive_response():
            print(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true", help="drive a real session (needs ANTHROPIC_API_KEY)"
    )
    args = parser.parse_args()
    if args.run:
        asyncio.run(run())
    else:
        print(describe())


if __name__ == "__main__":
    main()
