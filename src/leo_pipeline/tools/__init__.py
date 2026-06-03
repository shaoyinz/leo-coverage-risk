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
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from claude_agent_sdk import create_sdk_mcp_server, tool

from leo_pipeline import ingest
from leo_pipeline.config import DISCOVERY, INGESTION
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

# Optional process-level AOI override (lon/lat bbox [min_lon,min_lat,max_lon,max_lat]).
# The run_discovery CLI sets this so a user can target their own area without a locations
# CSV; None means "derive the AOI normally (CSV, else CONUS fallback)".
AOI_OVERRIDE: list[float] | None = None


@tool(
    "get_aoi_bbox",
    "Compute the area-of-interest bounding box (lon/lat) and point counts for the "
    "provided locations dataset, to use as the AOI of a data search. Read-only and takes "
    "no arguments. Honours a process-level AOI override (set by the run_discovery CLI's "
    "--bbox) if present, else derives the AOI from the locations CSV, else falls back to "
    "a CONUS bbox.",
    {},
)
async def get_aoi_bbox(args: dict[str, Any]) -> dict[str, Any]:
    if AOI_OVERRIDE is not None:
        # An override bbox targets a specific area, but if the locations CSV is present
        # we still report how many of its points actually fall inside that box — an
        # override is a spatial filter, not "no data". Hardcoding 0 here made the agent
        # write a misleading "CSV had 0 points" note even when the box was full of points.
        counts = {"n_total": 0, "n_distinct": 0}
        try:
            con = ingest.connect()
        except FileNotFoundError:
            pass
        else:
            try:
                counts = ingest.count_in_bbox(con, list(AOI_OVERRIDE))
            finally:
                con.close()
        return _text(
            {
                "bbox": list(AOI_OVERRIDE),
                "crs": "EPSG:4326",
                "n_total": counts["n_total"],
                "n_distinct": counts["n_distinct"],
                "source": "cli_override",
            }
        )
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


# Raw YAML for one dataset in the AWS Open Data Registry (awslabs/open-data-registry).
_REGISTRY_RAW_URL = (
    "https://raw.githubusercontent.com/awslabs/open-data-registry/main/datasets/{id}.yaml"
)


def _fetch_registry_record(registry_id: str, timeout: float = 8.0) -> dict[str, Any] | None:
    """Best-effort live fetch of one AWS Open Data Registry record, to *verify* a curated
    candidate resolves and to refresh its licence/owner from source. Returns the parsed
    Name/License/ManagedBy (+ ``resolved=True``) or ``None`` on any failure (offline, 404,
    no YAML parser) — callers fall back to the curated snapshot. Isolated as a module-level
    helper so tests can monkeypatch it without touching the network.
    """
    try:
        url = _REGISTRY_RAW_URL.format(id=registry_id)
        with urlopen(url, timeout=timeout) as resp:  # noqa: S310 (fixed https host)
            raw = resp.read().decode("utf-8", "replace")
    except Exception:
        return None
    try:
        import yaml  # optional dependency; absent -> treat as unverified

        doc = yaml.safe_load(raw) or {}
    except Exception:
        return None
    return {
        "name": doc.get("Name"),
        "license": doc.get("License"),
        "managed_by": doc.get("ManagedBy"),
        "resolved": True,
    }


@tool(
    "opendata_registry_search",
    "Search the curated AWS Open Data Registry snapshot for off-catalog obstruction "
    "datasets (e.g. tree-canopy HEIGHT, which no STAC catalog hosts) by keyword, and "
    "verify each hit against the live registry. This is the RELIABLE path for canopy and "
    "other non-STAC layers: it returns a grounded record (name, S3 URI, licence, vintage, "
    "resolution) deterministically, instead of depending on a general WebSearch that may "
    "be disabled or rank the real dataset poorly. `keywords` is a list of terms (e.g. "
    "['canopy','height']); `factor` optionally restricts to one obstruction factor "
    "('canopy'|'buildings'|...); `max_results` caps the hits (default 5). Use the returned "
    "`s3_uri`/`registry_url` as the manifest entry's source_url.",
    {"keywords": list, "factor": str, "max_results": int},
)
async def opendata_registry_search(args: dict[str, Any]) -> dict[str, Any]:
    keywords = [str(k).lower() for k in (args.get("keywords") or []) if str(k).strip()]
    factor_filter = (args.get("factor") or "").strip().lower() or None
    max_results = max(1, min(int(args.get("max_results") or 5), 25))

    # Flatten the curated catalog to (factor, entry) pairs, honouring an optional filter.
    catalog = DISCOVERY.web_sources or {}
    pool: list[tuple[str, dict[str, Any]]] = [
        (factor, entry)
        for factor, entries in catalog.items()
        if factor_filter is None or factor == factor_filter
        for entry in entries
    ]
    if not pool:
        return _text({"keywords": keywords, "n_results": 0, "results": []})

    def _score(factor: str, entry: dict[str, Any]) -> int:
        if not keywords:
            return 1  # no keywords -> return the whole (optionally filtered) pool
        haystack = " ".join(
            str(x).lower()
            for x in (
                factor,
                entry.get("name", ""),
                entry.get("description", ""),
                *entry.get("keywords", []),
            )
        )
        return sum(1 for k in keywords if k in haystack)

    ranked = sorted(
        ((_score(f, e), f, e) for f, e in pool), key=lambda t: t[0], reverse=True
    )
    hits = [(f, e) for score, f, e in ranked if score > 0][:max_results]

    results: list[dict[str, Any]] = []
    for factor, entry in hits:
        record = {
            "factor": factor,
            "name": entry.get("name"),
            "registry_id": entry.get("registry_id"),
            "registry_url": entry.get("registry_url"),
            "s3_uri": entry.get("s3_uri"),
            "gsd_m": entry.get("gsd_m"),
            "vintage": entry.get("vintage"),
            "license": entry.get("license"),
            "description": entry.get("description"),
            "verified": False,
            "managed_by": None,
        }
        rid = entry.get("registry_id")
        if rid:
            live = _fetch_registry_record(rid)
            if live and live.get("resolved"):
                record["verified"] = True
                record["managed_by"] = live.get("managed_by")
                if live.get("license"):  # prefer the source-of-truth licence string
                    record["license"] = live["license"]
        results.append(record)

    return _text({"keywords": keywords, "n_results": len(results), "results": results})


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

    # Grounding gate: an access=='web' pick must carry a resolvable source (source_url or
    # asset_href). This stops an ungrounded model recall ("I think Meta has a canopy layer")
    # from passing as a selected dataset — it is de-selected and the gap is surfaced to the
    # H1 reviewer rather than silently trusted. STAC picks are grounded by stac_search.
    grounding_notes: list[str] = []
    for c in candidates:
        if c.access == "web" and c.selected and not (c.source_url or c.asset_href):
            c.selected = False
            grounding_notes.append(
                f"de-selected ungrounded web pick {c.dataset_id!r} ({c.factor}): "
                "no source_url/asset_href — re-source via opendata_registry_search"
            )

    notes_list = notes if isinstance(notes, list) else [str(notes)]
    notes_list = list(notes_list) + grounding_notes

    manifest = DataManifest(
        aoi_bbox=list(bbox) if isinstance(bbox, (list, tuple)) else [],
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        entries=candidates,
        notes=notes_list,
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


# --- A2 surface-ingestion tools -------------------------------------------------------
# The ingestion agent reasons only about *which fallback to use on failure*; these tools do
# the deterministic geospatial work — windowed COG read, reproject to a metric CRS, fuse a
# pseudo-DSM — and write only a tile-keyed, content-addressed surface cache (no risk, no
# million-row payloads). See docs/architecture.md (A2, §3 fetch_aligned_surface, §4 state).


def _strip_query(href: str | None) -> str | None:
    """Drop a URL query string (e.g. a Planetary Computer SAS token) for cache keying.

    PC signing appends a short-lived SAS token that changes on every sign, so the raw
    signed href is useless as a cache key — two signings of the same asset must hash the
    same. We key on the asset path only.
    """
    if not href:
        return href
    return href.split("?", 1)[0]


def _sign_href(href: str | None) -> str | None:
    """Best-effort Planetary Computer signing; pass non-PC / unsignable hrefs through.

    Signing is needed at *download* time (A2), not in the persisted manifest (A1), because
    the SAS token is short-lived. Isolated as a module-level helper so tests can stub it.
    """
    if not href or not INGESTION.sign_assets:
        return href
    try:
        import planetary_computer

        return planetary_computer.sign(href)
    except Exception:
        return href  # not a PC url, offline, or signing unavailable -> use as-is


def _utm_epsg_for_bbox(bbox: list[float]) -> int:
    """UTM EPSG for a bbox centroid — the ``auto-UTM`` target CRS resolver."""
    from leo_pipeline.tiling import utm_epsg_for_lonlat

    lon = (float(bbox[0]) + float(bbox[2])) / 2.0
    lat = (float(bbox[1]) + float(bbox[3])) / 2.0
    return utm_epsg_for_lonlat(lon, lat)


def _resolve_target_crs(target_crs: str | None, bbox: list[float]) -> str:
    """Map ``auto-UTM`` (or empty) to the bbox's UTM zone; pass an explicit CRS through."""
    if not target_crs or str(target_crs).lower() in ("auto-utm", "auto"):
        return f"EPSG:{_utm_epsg_for_bbox(bbox)}"
    if isinstance(target_crs, int) or str(target_crs).isdigit():
        return f"EPSG:{int(target_crs)}"
    return str(target_crs)


def _manifest_hrefs(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalise the A1 ``manifest`` arg to ``factor -> {href, vintage}``.

    Accepts either ``{factor: href}`` or ``{factor: {asset_href|href, vintage}}`` and the
    common aliases (``surface``/``dsm``/``true_dsm`` for the lidar DSM, ``terrain``/``dem``
    for the bare-earth DEM). Returns only the factors actually present.
    """
    aliases = {
        "surface": ("surface", "dsm", "true_dsm"),
        "terrain": ("terrain", "dem"),
        "canopy": ("canopy",),
        "buildings": ("buildings",),
    }
    out: dict[str, dict[str, Any]] = {}
    for factor, keys in aliases.items():
        for key in keys:
            if key in manifest and manifest[key] is not None:
                val = manifest[key]
                if isinstance(val, dict):
                    href = val.get("asset_href") or val.get("href") or val.get("source_url")
                    vintage = val.get("vintage")
                else:
                    href, vintage = val, None
                if href:
                    out[factor] = {"href": str(href), "vintage": vintage}
                break
    return out


def _next_surface_mode(mode: str) -> str | None:
    """Next mode down the fallback hierarchy (true_dsm -> pseudo_dsm -> cover_proxy)."""
    modes = INGESTION.surface_modes
    if mode in modes:
        nxt = modes.index(mode) + 1
        return modes[nxt] if nxt < len(modes) else None
    return None


def _cache_key(
    tile_id: str,
    bbox: list[float],
    hrefs: dict[str, str | None],
    dst_crs: str,
    gsd_m: float,
    surface_mode: str,
) -> str:
    """Stable content hash of the fetch inputs (SAS tokens stripped) for idempotency."""
    payload = json.dumps(
        {
            "tile_id": tile_id,
            "bbox": [round(float(x), 6) for x in bbox],
            "hrefs": {k: _strip_query(v) for k, v in sorted(hrefs.items())},
            "crs": str(dst_crs),
            "gsd_m": float(gsd_m),
            "surface_mode": surface_mode,
        },
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def _read_window_reprojected(
    href: str, bbox4326: list[float], dst_crs: str, dst_gsd_m: float
) -> dict[str, Any]:
    """Windowed COG read + reproject onto a fixed metric grid covering ``bbox4326``.

    Reads only the source window overlapping the AOI (COG range requests via GDAL), then
    reprojects it to ``dst_crs`` at ``dst_gsd_m`` resolution. The destination grid is
    derived deterministically from ``(bbox4326, dst_crs, dst_gsd_m)`` alone, so two layers
    fetched with the same arguments land on an *identical* grid and can be fused
    pixel-for-pixel. Isolated as a module-level helper so offline tests stub it with small
    synthetic arrays (no network). Returns the array + georeferencing + nodata.
    """
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds as transform_from_bounds
    from rasterio.warp import Resampling, reproject, transform_bounds

    with rasterio.open(href) as src:
        dst_w, dst_s, dst_e, dst_n = transform_bounds(
            "EPSG:4326", dst_crs, *bbox4326, densify_pts=21
        )
        width = max(1, int(math.ceil((dst_e - dst_w) / dst_gsd_m)))
        height = max(1, int(math.ceil((dst_n - dst_s) / dst_gsd_m)))
        dst_transform = transform_from_bounds(dst_w, dst_s, dst_e, dst_n, width, height)
        nodata = src.nodata if src.nodata is not None else -9999.0

        src_w, src_s, src_e, src_n = transform_bounds(
            "EPSG:4326", src.crs, *bbox4326, densify_pts=21
        )
        window = src.window(src_w, src_s, src_e, src_n).round_offsets().round_lengths()
        src_arr = src.read(1, window=window, boundless=True, fill_value=nodata).astype(
            "float32"
        )
        src_transform = src.window_transform(window)

        dst_arr = np.full((height, width), nodata, dtype="float32")
        reproject(
            source=src_arr,
            destination=dst_arr,
            src_transform=src_transform,
            src_crs=src.crs,
            src_nodata=nodata,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            dst_nodata=nodata,
            resampling=Resampling.bilinear,
        )
    return {
        "array": dst_arr,
        "transform": dst_transform,
        "crs": str(dst_crs),
        "nodata": float(nodata),
        "gsd_m": float(dst_gsd_m),
        "shape": [height, width],
    }


def _fuse_pseudo_dsm(
    dem: Any, canopy: Any, dem_nodata: float, canopy_nodata: float
) -> Any:
    """pseudo-DSM = DEM + max(canopy_height, 0), nodata-aware.

    Where canopy is nodata it contributes 0 (bare earth — a conservative *clear* lean,
    consistent with treating absent obstruction data as no obstruction). Where the DEM
    itself is nodata the result is nodata (no terrain datum, nothing to build on).
    Buildings are DEFERRED in this live core: the building term is omitted, so a
    pseudo-DSM here is DEM + canopy only — documented as a known gap (see the agent prompt
    and docs/architecture.md "what this repo implements").
    """
    import numpy as np

    dem = np.asarray(dem, dtype="float32")
    canopy = np.asarray(canopy, dtype="float32")
    canopy_valid = np.isfinite(canopy) & (canopy != canopy_nodata)
    canopy_h = np.where(canopy_valid, np.maximum(canopy, 0.0), 0.0)
    fused = dem + canopy_h
    dem_invalid = ~np.isfinite(dem) | (dem == dem_nodata)
    fused = np.where(dem_invalid, dem_nodata, fused)
    return fused.astype("float32")


def _valid_fraction(array: Any, nodata: float) -> float:
    """Share of finite, non-nodata pixels — feeds coverage_flag / confidence."""
    import numpy as np

    arr = np.asarray(array)
    if arr.size == 0:
        return 0.0
    mask = np.isfinite(arr) & (arr != nodata)
    return float(mask.mean())


def _write_cog(path: Path, array: Any, transform: Any, crs: str, nodata: float) -> None:
    """Write a tiled, deflate-compressed single-band float32 GeoTIFF surface."""
    import rasterio

    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        dtype="float32",
        count=1,
        height=array.shape[0],
        width=array.shape[1],
        crs=crs,
        transform=transform,
        nodata=nodata,
        tiled=True,
        blockxsize=256,
        blockysize=256,
        compress="deflate",
    ) as dst:
        dst.write(array.astype("float32"), 1)


def _coverage_and_confidence(surface_mode: str, valid_frac: float) -> tuple[str, str]:
    """Map (mode, valid fraction) -> (coverage_flag, confidence).

    Confidence tracks the fallback hierarchy (true_dsm > pseudo_dsm > cover_proxy) and is
    knocked down one notch when the tile has gaps, so degraded surfaces stay usable but
    auditable rather than silently trusted (architecture §5)."""
    if surface_mode == "cover_proxy":
        return "cover_proxy", "low"
    if valid_frac >= 0.99:
        coverage = "ok"
    elif valid_frac > 0.0:
        coverage = "partial"
    else:
        coverage = "empty"
    base = {"true_dsm": "high", "pseudo_dsm": "medium"}.get(surface_mode, "low")
    if coverage != "ok":
        base = {"high": "medium", "medium": "low"}.get(base, "low")
    return coverage, base


@tool(
    "fetch_aligned_surface",
    "Build ONE aligned elevation surface for a tile from the H1-approved datasets: read "
    "the COG window(s) covering the tile bbox, reproject to a metric CRS, and either pass "
    "through a true lidar DSM or fuse DEM + canopy into a pseudo-DSM. Deterministic, "
    "cached and idempotent (re-running a tile with unchanged inputs is a no-op). You do "
    "NOT score risk here. `tile_id` names the cache entry; `bbox` is "
    "[min_lon,min_lat,max_lon,max_lat] in EPSG:4326; `manifest` maps obstruction factor -> "
    "asset href (keys: surface/dsm, terrain/dem, canopy; values are hrefs or "
    "{asset_href,vintage}); `target_crs` defaults to 'auto-UTM' (the tile's UTM zone); "
    "`target_gsd_m` is the output resolution (default 10); `surface_mode` is one of "
    "true_dsm|pseudo_dsm|cover_proxy (omit to auto-pick the best the manifest supports). "
    "On a read failure it returns an error naming the next fallback mode for you to retry. "
    "Returns {dsm_uri, dem_uri, crs, gsd_m, surface_mode, vintage_map, coverage_flag, "
    "confidence}.",
    {
        "tile_id": str,
        "bbox": list,
        "manifest": dict,
        "target_crs": str,
        "target_gsd_m": float,
        "surface_mode": str,
    },
)
async def fetch_aligned_surface(args: dict[str, Any]) -> dict[str, Any]:
    tile_id = (args.get("tile_id") or "").strip()
    bbox = args.get("bbox")
    manifest = args.get("manifest")
    target_gsd_m = float(args.get("target_gsd_m") or INGESTION.target_gsd_m)
    requested_mode = (args.get("surface_mode") or "").strip() or None

    if not tile_id:
        return _error("tile_id is required")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return _error("bbox must be [min_lon, min_lat, max_lon, max_lat]")
    try:
        bbox = [float(x) for x in bbox]
    except (TypeError, ValueError):
        return _error("bbox values must be numbers")
    if not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
        return _error("bbox must have min_lon<max_lon and min_lat<max_lat")
    if not isinstance(manifest, dict) or not manifest:
        return _error("manifest must be a non-empty {factor: asset_href} object from A1")
    if requested_mode and requested_mode not in INGESTION.surface_modes:
        return _error(
            f"surface_mode must be one of {list(INGESTION.surface_modes)}, "
            f"got {requested_mode!r}"
        )

    layers = _manifest_hrefs(manifest)
    dsm = layers.get("surface")
    dem = layers.get("terrain")
    canopy = layers.get("canopy")

    # Auto-pick the best mode the manifest can actually support, if not told one.
    if requested_mode:
        surface_mode = requested_mode
    elif dsm:
        surface_mode = "true_dsm"
    elif dem and canopy:
        surface_mode = "pseudo_dsm"
    elif dem:
        surface_mode = "cover_proxy"
    else:
        return _error(
            "manifest has none of: a surface/dsm href, or a terrain/dem href to build "
            "from — cannot ingest this tile"
        )

    # Confirm the chosen mode has its required layers; if not, point at the next fallback.
    need = {
        "true_dsm": ["surface"],
        "pseudo_dsm": ["terrain", "canopy"],
        "cover_proxy": ["terrain"],
    }[surface_mode]
    missing = [f for f in need if f not in layers]
    if missing:
        nxt = _next_surface_mode(surface_mode)
        hint = f" try surface_mode={nxt!r}" if nxt else ""
        return _error(
            f"surface_mode={surface_mode!r} needs manifest factor(s) {missing}, "
            f"not provided.{hint}"
        )

    target_crs = _resolve_target_crs(args.get("target_crs"), bbox)

    href_map = {
        "surface": dsm["href"] if dsm else None,
        "terrain": dem["href"] if dem else None,
        "canopy": canopy["href"] if canopy else None,
    }
    key = _cache_key(tile_id, bbox, href_map, target_crs, target_gsd_m, surface_mode)
    cache_dir = INGESTION.cache_dir
    base = cache_dir / f"{tile_id}__{key}"
    sidecar = base.with_suffix(".json")

    # Idempotency: a prior identical fetch already produced this surface -> return it.
    if sidecar.exists():
        try:
            cached = json.loads(sidecar.read_text())
            cached["cache_hit"] = True
            return _text(cached)
        except Exception:
            pass  # corrupt sidecar -> fall through and recompute

    vintage_map = {f: layers[f].get("vintage") for f in need}

    try:
        if surface_mode == "true_dsm":
            read = _read_window_reprojected(
                _sign_href(href_map["surface"]), bbox, target_crs, target_gsd_m
            )
            dsm_arr, nodata = read["array"], read["nodata"]
            transform = read["transform"]
            dem_uri = None
        elif surface_mode == "pseudo_dsm":
            dem_read = _read_window_reprojected(
                _sign_href(href_map["terrain"]), bbox, target_crs, target_gsd_m
            )
            canopy_read = _read_window_reprojected(
                _sign_href(href_map["canopy"]), bbox, target_crs, target_gsd_m
            )
            nodata = dem_read["nodata"]
            transform = dem_read["transform"]
            dsm_arr = _fuse_pseudo_dsm(
                dem_read["array"], canopy_read["array"], nodata, canopy_read["nodata"]
            )
        else:  # cover_proxy: DEM-only surface, no canopy/building term, low confidence
            dem_read = _read_window_reprojected(
                _sign_href(href_map["terrain"]), bbox, target_crs, target_gsd_m
            )
            dsm_arr, nodata = dem_read["array"], dem_read["nodata"]
            transform = dem_read["transform"]
            dem_uri = None
    except Exception as exc:
        nxt = _next_surface_mode(surface_mode)
        hint = f" Retry with surface_mode={nxt!r}." if nxt else ""
        return _error(
            f"read/reproject failed for surface_mode={surface_mode!r} on tile {tile_id}: "
            f"{exc}.{hint}"
        )

    valid_frac = _valid_fraction(dsm_arr, nodata)
    coverage_flag, confidence = _coverage_and_confidence(surface_mode, valid_frac)

    dsm_path = Path(str(base) + "_dsm.tif")
    _write_cog(dsm_path, dsm_arr, transform, target_crs, nodata)
    dem_uri = None
    if surface_mode == "pseudo_dsm":
        dem_path = Path(str(base) + "_dem.tif")
        _write_cog(dem_path, dem_read["array"], transform, target_crs, nodata)
        dem_uri = str(dem_path)

    payload = {
        "tile_id": tile_id,
        "dsm_uri": str(dsm_path),
        "dem_uri": dem_uri,
        "crs": target_crs,
        "gsd_m": target_gsd_m,
        "surface_mode": surface_mode,
        "vintage_map": vintage_map,
        "coverage_flag": coverage_flag,
        "confidence": confidence,
        "valid_fraction": round(valid_frac, 4),
        "shape": list(dsm_arr.shape),
        "cache_hit": False,
    }
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(payload, indent=2, default=str))
    return _text(payload)


@tool(
    "stac_item_read",
    "Resolve one obstruction factor's concrete, download-ready COG for a tile: search the "
    "STAC catalog for the best item covering the tile bbox and return its SIGNED asset "
    "href + metadata, ready to hand to fetch_aligned_surface. Use this to turn an A1 "
    "manifest collection (e.g. 'cop-dem-glo-30', '3dep-lidar-dsm') into the actual asset "
    "URL for THIS tile. `catalog` is a configured key or STAC root URL; `collection` is "
    "the collection id; `bbox` is [min_lon,min_lat,max_lon,max_lat]; `datetime` is an "
    "optional ISO interval; `sign` toggles Planetary Computer signing (default true). "
    "Returns {collection, asset_href, asset_key, crs, gsd_m, datetime, n_candidates}.",
    {"catalog": str, "collection": str, "bbox": list, "datetime": str, "sign": bool},
)
async def stac_item_read(args: dict[str, Any]) -> dict[str, Any]:
    catalog = args.get("catalog") or "planetary_computer"
    catalog_url = DISCOVERY.stac_catalogs.get(catalog, catalog)
    collection = (args.get("collection") or "").strip()
    bbox = args.get("bbox")
    dt = args.get("datetime") or None
    sign = args.get("sign")
    sign = True if sign is None else bool(sign)

    if not collection:
        return _error("collection id is required")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return _error("bbox must be [min_lon, min_lat, max_lon, max_lat]")

    try:
        from pystac_client import Client
    except ImportError as exc:  # pragma: no cover
        return _error(f"STAC libraries unavailable: {exc}")
    try:
        client = Client.open(catalog_url)
    except Exception as exc:
        return _error(f"could not open catalog {catalog_url!r}: {exc}")

    try:
        search = client.search(
            collections=[collection], bbox=bbox, datetime=dt, max_items=10
        )
        items = list(search.items())
    except Exception as exc:
        return _error(f"item search failed for {collection!r}: {exc}")

    if not items:
        return _text(
            {
                "collection": collection,
                "asset_href": None,
                "n_candidates": 0,
                "note": "no STAC items cover this tile bbox",
            }
        )

    # Best = finest resolution, newest as a tie-break (the A1 ranking criteria, per tile).
    def _sort_key(it: Any) -> tuple[float, str]:
        summ = _summarize_stac_item(it)
        gsd = summ.get("gsd_m")
        return (gsd if gsd is not None else 1e9, summ.get("datetime") or "")

    best = sorted(items, key=_sort_key)[0]
    summ = _summarize_stac_item(best)
    href = _sign_href(summ.get("asset_href")) if sign else summ.get("asset_href")
    return _text(
        {
            "collection": collection,
            "item_id": summ.get("id"),
            "asset_key": summ.get("asset_key"),
            "asset_href": href,
            "crs": summ.get("crs"),
            "gsd_m": summ.get("gsd_m"),
            "datetime": summ.get("datetime"),
            "n_candidates": len(items),
            "signed": bool(sign),
        }
    )


@tool(
    "cache_rw",
    "Inspect the tile-keyed surface cache so you can skip re-fetching an already-built "
    "tile (the cache is content-addressed and idempotent). `op` is 'check' (does a tile "
    "have a cached surface? pass `tile_id`) or 'list' (summarise all cached surfaces). "
    "Returns the matching surface payload(s) — the same {dsm_uri, dem_uri, crs, gsd_m, "
    "surface_mode, ...} fetch_aligned_surface would return.",
    {"op": str, "tile_id": str},
)
async def cache_rw(args: dict[str, Any]) -> dict[str, Any]:
    op = (args.get("op") or "list").strip().lower()
    tile_id = (args.get("tile_id") or "").strip()
    cache_dir = INGESTION.cache_dir

    if op not in ("check", "list"):
        return _error("op must be 'check' or 'list'")
    if op == "check" and not tile_id:
        return _error("op='check' requires a tile_id")
    if not cache_dir.exists():
        return _text({"op": op, "cache_dir": str(cache_dir), "n_found": 0, "surfaces": []})

    pattern = f"{tile_id}__*.json" if op == "check" else "*.json"
    surfaces: list[dict[str, Any]] = []
    for sidecar in sorted(cache_dir.glob(pattern)):
        try:
            surfaces.append(json.loads(sidecar.read_text()))
        except Exception:
            continue
    return _text(
        {
            "op": op,
            "cache_dir": str(cache_dir),
            "n_found": len(surfaces),
            "surfaces": surfaces,
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
        opendata_registry_search,
        write_data_manifest,
        fetch_aligned_surface,
        stac_item_read,
        cache_rw,
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
    "mcp__leo__opendata_registry_search",
    "mcp__leo__write_data_manifest",
    "mcp__leo__fetch_aligned_surface",
    "mcp__leo__stac_item_read",
    "mcp__leo__cache_rw",
    "mcp__leo__lookup_obstruction_layer",
    "mcp__leo__compute_risk_score",
]
