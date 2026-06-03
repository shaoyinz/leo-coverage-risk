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

# --- Data-discovery agent (A1) --------------------------------------------------------
# Scope: source the environmental obstruction datasets for the AOI and produce a ranked,
# human-reviewable data manifest. Reasons/ranks only — never downloads rasters.
DATA_DISCOVERY_AGENT = AgentDefinition(
    description="Searches STAC catalogs + the web for obstruction datasets and writes a ranked data manifest.",
    prompt=(
        "You are the data-discovery agent (A1 in docs/architecture.md). Your job is to "
        "find the public geospatial datasets needed to model sky obstruction for the "
        "input locations, rank them, and write ONE ranked data manifest for human "
        "approval (the H1 gate). You do NOT download rasters or compute risk — your only "
        "artifact is the manifest.\n\n"
        "The methodology (docs/rationale.md) needs a fused surface = terrain + the taller "
        "of canopy/buildings, so source four obstruction factors:\n"
        "  - terrain: bare-earth DEM\n"
        "  - surface: lidar-derived DSM where it exists (preferred; first-return surface)\n"
        "  - canopy: tree-canopy HEIGHT (not percent-cover — cover cannot give a horizon)\n"
        "  - buildings: building footprints, ideally with height\n"
        "Honour the fallback hierarchy: true lidar DSM > DEM + max(canopy height, building "
        "height) > coarse cover proxy.\n\n"
        "Workflow:\n"
        "1. Call get_aoi_bbox to get the AOI (the bounding box of the input locations).\n"
        "2. For terrain/surface/buildings, call stac_search against the 'planetary_computer' "
        "catalog over the AOI. Start from these candidate collections and compare them: "
        "terrain -> ['3dep-seamless','cop-dem-glo-30','nasadem']; surface -> "
        "['3dep-lidar-dsm']; buildings -> ['ms-buildings']. Rank by RESOLUTION (gsd_m, "
        "smaller is better), VINTAGE (newer datetime), AOI COVERAGE (aoi_coverage_pct), and "
        "LICENCE/cost. Note that 3DEP is high-res but CONUS-only, while Copernicus/NASADEM "
        "are global fallbacks — pick the best where it covers the AOI and a global fallback "
        "elsewhere.\n"
        "3. Canopy HEIGHT is NOT in Planetary Computer. Use WebSearch/WebFetch to find a "
        "canopy-height source (e.g. Meta/WRI 1 m or ETH GlobalCanopyHeight 10 m) and record "
        "its access method, resolution, vintage, and LICENCE.\n"
        "4. Call write_data_manifest with the AOI and a list of entries — include the "
        "SELECTED dataset per factor AND the notable rejected alternatives (selected=false), "
        "each with a one-line rationale. Set access='stac' for catalog hits (with collection "
        "and a representative unsigned asset_href) and access='web' for off-catalog sources. "
        "Put licence concerns, coverage gaps, and any factor you could not source into "
        "`notes` — these are exactly what the H1 reviewer must sign off before bulk download."
    ),
    tools=[
        "mcp__leo__get_aoi_bbox",
        "mcp__leo__stac_search",
        "mcp__leo__write_data_manifest",
        "WebSearch",
        "WebFetch",
    ],
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
        "data-discovery": DATA_DISCOVERY_AGENT,
        "geo-analysis": GEO_ANALYSIS_AGENT,
        "qa": QA_AGENT,
    }


__all__ = [
    "INGESTION_AGENT",
    "DATA_DISCOVERY_AGENT",
    "GEO_ANALYSIS_AGENT",
    "QA_AGENT",
    "all_agents",
    "MODELS",
]
