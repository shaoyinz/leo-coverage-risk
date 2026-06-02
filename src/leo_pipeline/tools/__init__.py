"""Custom tools exposed to the agents, as an in-process SDK MCP server.

``query_locations`` and ``deduplicate_coordinates`` are live: they run against the
provided locations dataset via DuckDB. The obstruction/risk tools are still thin
stubs (real name, description, and input schema — the "tool definitions with
schemas" deliverable) pending the environmental datasets; fill in their bodies in
subsequent steps.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from leo_pipeline import ingest

# Statements the read-only query tool will accept. Anything else (INSERT, COPY,
# CREATE, ATTACH, PRAGMA, ...) is rejected so an agent can't mutate state or touch
# the filesystem through the SQL surface.
_READ_ONLY_PREFIXES = ("select", "with")
_MAX_ROWS = 1000


def _stub(name: str, **echo: Any) -> dict[str, Any]:
    """Uniform placeholder response so callers can see the tool wiring works."""
    detail = ", ".join(f"{k}={v!r}" for k, v in echo.items())
    return {
        "content": [
            {"type": "text", "text": f"[stub] {name} not yet implemented. args: {detail}"}
        ]
    }


def _text(payload: Any) -> dict[str, Any]:
    """Wrap a JSON-serialisable payload as an MCP text result."""
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {message}"}], "is_error": True}


@tool(
    "query_locations",
    "Run a read-only SQL query (DuckDB dialect) against the provided locations "
    "dataset, exposed as the table `locations` (columns: location_id, latitude, "
    "longitude, geoid_cb). Use for profiling, filtering, and aggregating the "
    "~4.7M-row input. Only SELECT/WITH statements are allowed; results are capped.",
    {"sql": str, "limit": int},
)
async def query_locations(args: dict[str, Any]) -> dict[str, Any]:
    sql = (args.get("sql") or "").strip().rstrip(";").strip()
    if not sql:
        return _error("empty sql")
    if not sql.lower().startswith(_READ_ONLY_PREFIXES):
        return _error("only read-only SELECT/WITH queries are allowed")
    if ";" in sql:
        return _error("multiple statements are not allowed")
    limit = args.get("limit") or 100
    limit = max(1, min(int(limit), _MAX_ROWS))

    con = ingest.connect()
    try:
        cur = con.execute(f"SELECT * FROM ({sql}) AS _q LIMIT {limit}")
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    except Exception as exc:  # surface DuckDB errors back to the agent, don't crash the tool
        return _error(str(exc))
    finally:
        con.close()
    return _text({"columns": columns, "row_count": len(rows), "rows": rows})


@tool(
    "deduplicate_coordinates",
    "Collapse the location-grained input to one work item per unique coordinate so "
    "the expensive per-coordinate obstruction sampling runs once instead of once per "
    "location. Writes unique_coords.parquet (the work list) and "
    "location_coord_map.parquet (the location_id->coord_id fan-out map) to "
    "data/interim, and returns the reduction summary. `precision` is the coordinate "
    "decimal places to round to before de-duplicating (default 6 ~= 0.11m; lower "
    "values snap near-coincident points to a shared grid cell).",
    {"precision": int},
)
async def deduplicate_coordinates(args: dict[str, Any]) -> dict[str, Any]:
    precision = args.get("precision")
    precision = ingest.DEFAULT_PRECISION if precision is None else int(precision)
    con = ingest.connect()
    try:
        result = ingest.deduplicate_coordinates(con, precision=precision)
    except Exception as exc:
        return _error(str(exc))
    finally:
        con.close()
    return _text(
        {
            "precision": result.precision,
            "total_locations": result.total_locations,
            "unique_coords": result.unique_coords,
            "duplicate_locations": result.duplicate_locations,
            "reduction_pct": round(result.reduction_pct, 2),
            "unique_coords_path": str(result.unique_coords_path),
            "location_map_path": str(result.location_map_path),
        }
    )


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
    tools=[
        query_locations,
        deduplicate_coordinates,
        lookup_obstruction_layer,
        compute_risk_score,
    ],
)

# Fully-qualified tool identifiers for ClaudeAgentOptions(allowed_tools=...).
TOOL_NAMES = [
    "mcp__leo__query_locations",
    "mcp__leo__deduplicate_coordinates",
    "mcp__leo__lookup_obstruction_layer",
    "mcp__leo__compute_risk_score",
]
