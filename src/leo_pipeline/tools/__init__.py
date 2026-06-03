"""Custom tools exposed to the agents, as an in-process SDK MCP server.

Live tools: ``query_locations`` and ``deduplicate_coordinates`` (DuckDB over the
provided locations dataset), and the A1 data-discovery trio ``get_aoi_bbox`` /
``stac_search`` / ``write_data_manifest`` (live STAC catalog search + manifest
persistence). The obstruction/risk tools (``lookup_obstruction_layer``,
``compute_risk_score``) are still thin stubs (real name, description, and input
schema — the "tool definitions with schemas" deliverable) pending the per-coordinate
sampling work; fill in their bodies in subsequent steps.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from leo_pipeline import ingest
from leo_pipeline.config import DISCOVERY
from leo_pipeline.state import DataManifest, DatasetCandidate

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


# --- A1 data-discovery tools ----------------------------------------------------------
# The discovery agent reasons and ranks; these tools do the deterministic catalog work
# and write only the manifest (no rasters). See docs/architecture.md (A1).


@tool(
    "get_aoi_bbox",
    "Compute the area-of-interest bounding box (lon/lat) and point counts for the "
    "provided locations dataset, to use as the AOI of a data search. Read-only and takes "
    "no arguments. Falls back to a CONUS bbox if the locations CSV is absent.",
    {},
)
async def get_aoi_bbox(args: dict[str, Any]) -> dict[str, Any]:
    try:
        con = ingest.connect()
    except FileNotFoundError:
        return _text(
            {
                "bbox": list(DISCOVERY.default_aoi_bbox),
                "crs": "EPSG:4326",
                "n_total": 0,
                "n_distinct": 0,
                "source": "default_conus_fallback",
            }
        )
    try:
        aoi = ingest.aoi_bbox(con)
    except Exception as exc:
        return _error(str(exc))
    finally:
        con.close()
    aoi["source"] = "locations_csv"
    return _text(aoi)


def _summarize_stac_item(item: Any) -> dict[str, Any]:
    """Ranking-relevant metadata from a STAC item: CRS, resolution, date, a
    representative asset href. Hrefs are returned UNSIGNED — Planetary Computer signing
    is short-lived and belongs at download time (A2), not in a persisted manifest.
    """
    props = item.properties or {}
    epsg = props.get("proj:epsg")
    assets = item.assets or {}
    asset_key = href = None
    for key in ("data", "dsm", "dem", "elevation", "cog", "image", "visual"):
        if key in assets:
            asset_key, href = key, assets[key].href
            break
    if href is None and assets:
        asset_key = next(iter(assets))
        href = assets[asset_key].href
    return {
        "id": item.id,
        "asset_key": asset_key,
        "asset_href": href,
        "crs": f"EPSG:{epsg}" if epsg else None,
        "gsd_m": props.get("gsd"),
        "datetime": props.get("datetime") or props.get("start_datetime"),
    }


@tool(
    "stac_search",
    "Probe a STAC catalog for assets covering an AOI — a coverage/metadata check, NOT a "
    "tile download. For each requested collection it returns the collection's spatial "
    "extent, the % of the AOI it covers, and a small sample of items (asset href, CRS, "
    "resolution, date) so you can rank datasets by resolution / vintage / coverage. "
    "`catalog` is a configured key (e.g. 'planetary_computer') or a STAC root URL; "
    "`bbox` is [min_lon,min_lat,max_lon,max_lat] in EPSG:4326; `collections` is a list "
    "of collection ids; `datetime` is an optional ISO interval; `max_items` caps the "
    "per-collection sample (default 10).",
    {"catalog": str, "collections": list, "bbox": list, "datetime": str, "max_items": int},
)
async def stac_search(args: dict[str, Any]) -> dict[str, Any]:
    catalog = args.get("catalog") or "planetary_computer"
    catalog_url = DISCOVERY.stac_catalogs.get(catalog, catalog)
    collections = args.get("collections") or []
    bbox = args.get("bbox")
    dt = args.get("datetime") or None
    max_items = max(1, min(int(args.get("max_items") or 10), 50))

    if not isinstance(bbox, list) or len(bbox) != 4:
        return _error("bbox must be [min_lon, min_lat, max_lon, max_lat]")
    if not collections:
        return _error("at least one collection id is required")

    try:
        from pystac_client import Client
        from shapely.geometry import box
    except ImportError as exc:  # pragma: no cover
        return _error(f"STAC libraries unavailable: {exc}")

    try:
        client = Client.open(catalog_url)
    except Exception as exc:
        return _error(f"could not open catalog {catalog_url!r}: {exc}")

    aoi_geom = box(*bbox)
    aoi_area = aoi_geom.area or 1.0
    results = []
    for coll_id in collections:
        entry: dict[str, Any] = {
            "collection": coll_id,
            "collection_extent_bbox": None,
            "aoi_coverage_pct": None,
            "n_items_sampled": 0,
            "items": [],
            "error": None,
        }
        try:
            coll = client.get_collection(coll_id)
            bboxes = coll.extent.spatial.bboxes if coll.extent and coll.extent.spatial else None
            if bboxes:
                ext = bboxes[0]
                entry["collection_extent_bbox"] = list(ext)
                inter = aoi_geom.intersection(box(ext[0], ext[1], ext[2], ext[3])).area
                entry["aoi_coverage_pct"] = round(100.0 * inter / aoi_area, 2)
        except Exception as exc:
            entry["error"] = f"collection lookup failed: {exc}"
        try:
            search = client.search(
                collections=[coll_id], bbox=bbox, datetime=dt, max_items=max_items
            )
            items = list(search.items())
            entry["n_items_sampled"] = len(items)
            entry["items"] = [_summarize_stac_item(it) for it in items]
        except Exception as exc:
            prefix = f"{entry['error']}; " if entry["error"] else ""
            entry["error"] = f"{prefix}item search failed: {exc}"
        results.append(entry)

    return _text({"catalog": catalog_url, "bbox": bbox, "collections": results})


@tool(
    "write_data_manifest",
    "Persist the ranked data manifest (the A1 deliverable) to "
    "data/interim/data_manifest.json for the H1 human-review gate. `aoi` is the dict "
    "from get_aoi_bbox; `entries` is a list of dataset candidates (include rejected ones "
    "too), each with at least factor, dataset_id, access ('stac'|'web') and selected "
    "(bool), plus ranking metadata (gsd_m, vintage, coverage_pct, license, rationale); "
    "`notes` is a list of licensing flags / coverage gaps.",
    {"aoi": dict, "entries": list, "notes": list},
)
async def write_data_manifest(args: dict[str, Any]) -> dict[str, Any]:
    aoi = args.get("aoi") or {}
    bbox = aoi.get("bbox") if isinstance(aoi, dict) else aoi
    entries = args.get("entries")
    notes = args.get("notes") or []

    if not isinstance(entries, list) or not entries:
        return _error("entries must be a non-empty list of dataset candidates")

    required = {"factor", "dataset_id", "access", "selected"}
    valid = {f.name for f in dataclasses.fields(DatasetCandidate)}
    candidates: list[DatasetCandidate] = []
    for e in entries:
        if not isinstance(e, dict):
            return _error("each entry must be an object")
        missing = required - e.keys()
        if missing:
            return _error(f"entry missing required keys {sorted(missing)}: {e}")
        candidates.append(DatasetCandidate(**{k: v for k, v in e.items() if k in valid}))

    manifest = DataManifest(
        aoi_bbox=list(bbox) if isinstance(bbox, (list, tuple)) else [],
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        entries=candidates,
        notes=notes if isinstance(notes, list) else [str(notes)],
    )
    path = DISCOVERY.manifest_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataclasses.asdict(manifest), indent=2, default=str))

    selected_factors = sorted({c.factor for c in candidates if c.selected})
    factors_missing = [
        f for f in ("terrain", "surface", "canopy", "buildings") if f not in selected_factors
    ]
    return _text(
        {
            "path": str(path),
            "n_entries": len(candidates),
            "factors_covered": selected_factors,
            "factors_missing": factors_missing,
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
        get_aoi_bbox,
        stac_search,
        write_data_manifest,
        lookup_obstruction_layer,
        compute_risk_score,
    ],
)

# Fully-qualified tool identifiers for ClaudeAgentOptions(allowed_tools=...).
TOOL_NAMES = [
    "mcp__leo__query_locations",
    "mcp__leo__deduplicate_coordinates",
    "mcp__leo__get_aoi_bbox",
    "mcp__leo__stac_search",
    "mcp__leo__write_data_manifest",
    "mcp__leo__lookup_obstruction_layer",
    "mcp__leo__compute_risk_score",
]
