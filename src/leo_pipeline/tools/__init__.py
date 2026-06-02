"""Custom tools exposed to the agents, as an in-process SDK MCP server.

Tools are intentionally thin stubs at this stage: each has a real name, description,
and input schema (the "tool definitions with schemas" deliverable) but returns a
``not-yet-implemented`` marker so the wiring can be tested before the analysis logic
lands. Fill in the bodies in subsequent steps.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool


def _stub(name: str, **echo: Any) -> dict[str, Any]:
    """Uniform placeholder response so callers can see the tool wiring works."""
    detail = ", ".join(f"{k}={v!r}" for k, v in echo.items())
    return {
        "content": [
            {"type": "text", "text": f"[stub] {name} not yet implemented. args: {detail}"}
        ]
    }


@tool(
    "query_locations",
    "Run a read-only SQL query (DuckDB dialect) against the provided locations "
    "dataset and return rows as JSON. Use for profiling, filtering, and aggregating "
    "the ~1M-coordinate input.",
    {"sql": str, "limit": int},
)
async def query_locations(args: dict[str, Any]) -> dict[str, Any]:
    # TODO: execute against data/raw/locations.csv via duckdb, enforce read-only + LIMIT.
    return _stub("query_locations", sql=args.get("sql"), limit=args.get("limit"))


@tool(
    "lookup_obstruction_layer",
    "Sample an environmental obstruction layer (canopy height, terrain/DEM slope, or "
    "building footprints) at a coordinate and return the value plus source metadata.",
    {"lat": float, "lon": float, "layer": str},
)
async def lookup_obstruction_layer(args: dict[str, Any]) -> dict[str, Any]:
    # TODO: sample raster/vector layer (rasterio/geopandas) for the requested layer.
    return _stub(
        "lookup_obstruction_layer",
        lat=args.get("lat"),
        lon=args.get("lon"),
        layer=args.get("layer"),
    )


@tool(
    "compute_risk_score",
    "Combine sampled obstruction factors into a 0..1 risk score and a low/medium/high "
    "band, applying the methodology documented in docs/rationale.md.",
    {"factors": dict},
)
async def compute_risk_score(args: dict[str, Any]) -> dict[str, Any]:
    # TODO: weighted model translating install-guide thresholds into a score.
    return _stub("compute_risk_score", factors=args.get("factors"))


# In-process MCP server bundling the tools above. Reference its name in
# ClaudeAgentOptions(mcp_servers=...) and allow the "mcp__leo__<tool>" identifiers.
LEO_TOOLS_SERVER = create_sdk_mcp_server(
    name="leo",
    version="0.1.0",
    tools=[query_locations, lookup_obstruction_layer, compute_risk_score],
)

# Fully-qualified tool identifiers for ClaudeAgentOptions(allowed_tools=...).
TOOL_NAMES = [
    "mcp__leo__query_locations",
    "mcp__leo__lookup_obstruction_layer",
    "mcp__leo__compute_risk_score",
]
