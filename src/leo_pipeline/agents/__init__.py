"""Agent definitions (subagents) for the pipeline.

Boundaries only at this stage: each agent has a scope, a system prompt sketch, and an
explicit allow-list of tools. The orchestrator wires these into ClaudeAgentOptions.
Tool access is deliberately least-privilege per agent.
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from leo_pipeline.config import MODELS

# --- Ingestion & data-quality agent ---------------------------------------------------
# Scope: load the provided locations CSV, profile it, and flag quality issues.
# Tools: query_locations only (read-only SQL). No obstruction/raster access.
INGESTION_AGENT = AgentDefinition(
    description="Loads and profiles the provided locations dataset; flags data-quality issues.",
    prompt=(
        "You are the data-ingestion agent. Profile the provided locations dataset using "
        "the query_locations tool: row counts, null/duplicate coordinates, out-of-range "
        "lat/lon, and other anomalies. Report findings as structured quality issues. Then "
        "call deduplicate_coordinates to collapse the location-grained input to one work "
        "item per unique coordinate (writing the unique-coordinate work list and the "
        "location_id->coord_id fan-out map to data/interim) so the downstream obstruction "
        "sampling runs once per coordinate, not once per location. Report the reduction. "
        "Do not attempt risk analysis."
    ),
    tools=["mcp__leo__query_locations", "mcp__leo__deduplicate_coordinates"],
    model="inherit",
)

# --- Geospatial analysis agent --------------------------------------------------------
# Scope: for clean locations, sample obstruction layers and compute risk scores.
GEO_ANALYSIS_AGENT = AgentDefinition(
    description="Samples obstruction layers per location and computes connectivity risk scores.",
    prompt=(
        "You are the geospatial-analysis agent. For each clean location, sample the relevant "
        "obstruction layers (canopy, terrain, structures) with lookup_obstruction_layer, then "
        "call compute_risk_score to produce a 0..1 score and a low/medium/high band. Follow the "
        "methodology in docs/rationale.md. Flag locations the public data cannot model."
    ),
    tools=[
        "mcp__leo__query_locations",
        "mcp__leo__lookup_obstruction_layer",
        "mcp__leo__compute_risk_score",
    ],
    model="inherit",
)

# --- QA / validation agent ------------------------------------------------------------
# Scope: sanity-check analysis outputs, catch anomalous results before reporting.
QA_AGENT = AgentDefinition(
    description="Validates analysis outputs for anomalies and internal consistency before reporting.",
    prompt=(
        "You are the QA agent. Review the risk findings for anomalies: implausible score "
        "distributions, missing coverage, contradictions with the data-quality report. Approve "
        "the run or send it back with specific reasons. You have read-only query access."
    ),
    tools=["mcp__leo__query_locations"],
    model="inherit",
)


def all_agents() -> dict[str, AgentDefinition]:
    """Map of agent name -> definition for ClaudeAgentOptions(agents=...)."""
    return {
        "ingestion": INGESTION_AGENT,
        "geo-analysis": GEO_ANALYSIS_AGENT,
        "qa": QA_AGENT,
    }


__all__ = ["INGESTION_AGENT", "GEO_ANALYSIS_AGENT", "QA_AGENT", "all_agents", "MODELS"]
