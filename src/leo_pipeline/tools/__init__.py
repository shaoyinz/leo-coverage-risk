"""Custom tools exposed to the agents, as an in-process SDK MCP server.

Live tools: ``query_locations`` and ``deduplicate_coordinates`` (DuckDB over the
provided locations dataset); the A1 data-discovery trio ``get_aoi_bbox`` /
``stac_search`` / ``write_data_manifest`` (live STAC catalog search + manifest
persistence); the A2 surface-ingestion tools ``stac_item_read`` /
``fetch_aligned_surface`` / ``cache_rw`` (windowed COG read + reproject + fuse); and
the A3 analysis tools ``compute_sky_obstruction`` / ``find_clear_sky_spot`` (the
per-azimuth horizon-profile obstruction scoring in ``leo_pipeline.horizon``); and the
A4 QA tools ``qa_input_audit`` / ``qa_location_batch`` (deterministic input-quality +
output-anomaly checks in ``leo_pipeline.qa``); and the A5 reporting tools
``aggregate_findings`` / ``render_map`` / ``write_report`` (county+state rollups, the
PMTiles+MapLibre interactive map, and the officer decision log in ``leo_pipeline.report``).
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
from leo_pipeline import qa as qa_engine
from leo_pipeline import report as report_engine
from leo_pipeline.config import ANALYSIS, DISCOVERY, INGESTION, QA, REPORTING
from leo_pipeline.state import DataManifest, DatasetCandidate

# Statements the read-only query tool will accept. Anything else (INSERT, COPY,
# CREATE, ATTACH, PRAGMA, ...) is rejected so an agent can't mutate state or touch
# the filesystem through the SQL surface.
_READ_ONLY_PREFIXES = ("select", "with")
_MAX_ROWS = 1000


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


# Manifest-carried OBM access keys — what makes the building source *manifest-driven* rather
# than config-bound: the tiled-URL template (with a ``{quadkey}`` placeholder), the quadkey zoom
# level, and the GeoParquet column names. Any key a manifest entry omits falls back to the
# matching INGESTION config default in :func:`_building_access`.
_BUILDING_ACCESS_KEYS = ("url_template", "quadkey_zoom", "geom_col", "height_col", "bbox_col")


def _manifest_hrefs(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalise the A1 ``manifest`` arg to ``factor -> {hrefs, vintage}``.

    Accepts either ``{factor: href}``, ``{factor: [href, ...]}`` (a granule mosaic for one
    source), or ``{factor: {asset_href|href, vintage}}`` and the common aliases
    (``surface``/``dsm``/``true_dsm`` for the lidar DSM, ``terrain``/``dem`` for the
    bare-earth DEM). ``hrefs`` is always a list (one entry for a single granule, several when
    a tile spans a granule boundary). Returns only the factors actually present.

    The ``buildings`` factor is special: OBM is vector GeoParquet on a quadkey grid, not a
    single COG, so its *presence* is the fusion on-switch and the access pattern rides on the
    entry (``url_template``/``quadkey_zoom``/column names — see ``_BUILDING_ACCESS_KEYS``).
    Those keys are passed through here and resolved by :func:`_building_access`, so a buildings
    entry counts as present even when it carries no plain href.
    """
    aliases = {
        "surface": ("surface", "dsm", "true_dsm"),
        "terrain": ("terrain", "dem"),
        "canopy": ("canopy",),
        "buildings": ("buildings",),
    }

    def _as_list(h: Any) -> list[str]:
        if h is None:
            return []
        if isinstance(h, (list, tuple)):
            return [str(x) for x in h if x]
        return [str(h)]

    out: dict[str, dict[str, Any]] = {}
    for factor, keys in aliases.items():
        for key in keys:
            if key in manifest and manifest[key] is not None:
                val = manifest[key]
                if isinstance(val, dict):
                    href = val.get("asset_href") or val.get("href") or val.get("source_url")
                    vintage = val.get("vintage")
                    extra = {k: val[k] for k in _BUILDING_ACCESS_KEYS if val.get(k) is not None}
                else:
                    href, vintage, extra = val, None, {}
                hrefs = _as_list(href)
                if factor == "buildings":
                    out[factor] = {"hrefs": hrefs, "vintage": vintage, **extra}
                elif hrefs:
                    out[factor] = {"hrefs": hrefs, "vintage": vintage}
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
    def _key_href(v: Any) -> Any:
        # A factor's value may be a single href (str), a granule-mosaic list, or None.
        if isinstance(v, (list, tuple)):
            return [_strip_query(x) for x in v]
        return _strip_query(v)

    payload = json.dumps(
        {
            "tile_id": tile_id,
            "bbox": [round(float(x), 6) for x in bbox],
            "hrefs": {k: _key_href(v) for k, v in sorted(hrefs.items())},
            "crs": str(dst_crs),
            "gsd_m": float(gsd_m),
            "surface_mode": surface_mode,
        },
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def _dst_grid(
    bbox4326: list[float], dst_crs: str, dst_gsd_m: float
) -> tuple[Any, int, int, tuple[float, float, float, float]]:
    """The fixed destination grid covering ``bbox4326`` in ``dst_crs`` at ``dst_gsd_m``.

    Derived from ``(bbox4326, dst_crs, dst_gsd_m)`` alone so every layer built with the same
    arguments — a reprojected COG window OR a rasterized building footprint — lands on a
    *bit-identical* grid and fuses pixel-for-pixel. Returns ``(transform, width, height,
    (w, s, e, n))`` (bounds in ``dst_crs``).
    """
    from rasterio.transform import from_bounds as transform_from_bounds
    from rasterio.warp import transform_bounds

    dst_w, dst_s, dst_e, dst_n = transform_bounds(
        "EPSG:4326", dst_crs, *bbox4326, densify_pts=21
    )
    width = max(1, int(math.ceil((dst_e - dst_w) / dst_gsd_m)))
    height = max(1, int(math.ceil((dst_n - dst_s) / dst_gsd_m)))
    dst_transform = transform_from_bounds(dst_w, dst_s, dst_e, dst_n, width, height)
    return dst_transform, width, height, (dst_w, dst_s, dst_e, dst_n)


def _read_window_reprojected(
    href: str, bbox4326: list[float], dst_crs: str, dst_gsd_m: float
) -> dict[str, Any]:
    """Windowed COG read + reproject onto a fixed metric grid covering ``bbox4326``.

    Reads only the source window overlapping the AOI (COG range requests via GDAL), then
    reprojects it to ``dst_crs`` at ``dst_gsd_m`` resolution. The destination grid is
    derived deterministically from ``(bbox4326, dst_crs, dst_gsd_m)`` (via :func:`_dst_grid`),
    so two layers fetched with the same arguments land on an *identical* grid and can be fused
    pixel-for-pixel. Isolated as a module-level helper so offline tests stub it with small
    synthetic arrays (no network). Returns the array + georeferencing + nodata.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject, transform_bounds

    with rasterio.open(href) as src:
        dst_transform, width, height, _ = _dst_grid(bbox4326, dst_crs, dst_gsd_m)
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


def _read_window_mosaic(
    hrefs: list[str], bbox4326: list[float], dst_crs: str, dst_gsd_m: float
) -> dict[str, Any]:
    """Read several COG granules and coalesce them into one layer on the tile grid.

    A single STAC item is one ~1° granule; a 5 km tile that straddles a granule boundary is
    only partially covered by any one item, which is what leaves the DEM base full of holes
    (→ spurious ``undetermined``). Reading **every** intersecting granule and filling each
    other's nodata gaps restores full coverage. Each granule is reprojected onto the SAME
    ``(bbox, crs, gsd)``-derived grid (via :func:`_read_window_reprojected`), so they overlay
    pixel-for-pixel; granules are coalesced first-valid-wins (callers pass finest-resolution
    first). A one-element list is just a passthrough, so single-granule sources are unchanged.
    """
    import numpy as np

    base: dict[str, Any] | None = None
    for href in hrefs:
        layer = _read_window_reprojected(href, bbox4326, dst_crs, dst_gsd_m)
        if base is None:
            base = layer
            continue
        out = base["array"]
        fill = layer["array"]
        # Fill only where the accumulator is still nodata and this granule has a real value.
        holes = (out == base["nodata"]) & (fill != layer["nodata"])
        out[holes] = fill[holes]
    if base is None:
        raise ValueError("no hrefs to mosaic")
    return base


# --- Building-height layer (OpenBuildingMap) ------------------------------------------------
# OBM is vector GeoParquet, not a COG, so it gets its own read path: pick the level-6 quadkey
# file(s) covering the tile, pull building polygons + a GEM-taxonomy height string within the
# bbox (anonymous HTTPS via DuckDB), parse the height to metres, and rasterize onto the SAME
# (bbox, crs, gsd) grid the COG reads use so it composites pixel-for-pixel. Building height is
# the modelled obstruction term OUTSIDE lidar coverage (the lidar DSM already carries roofs).


def _lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Web-Mercator slippy tile (x, y) containing ``(lon, lat)`` at ``zoom``."""
    n = 2 ** zoom
    x = int((float(lon) + 180.0) / 360.0 * n)
    lat_rad = math.radians(float(lat))
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def _tile_to_quadkey(x: int, y: int, zoom: int) -> str:
    """Bing/OBM quadkey string for slippy tile ``(x, y)`` at ``zoom``."""
    out = []
    for i in range(zoom, 0, -1):
        mask = 1 << (i - 1)
        digit = (1 if x & mask else 0) + (2 if y & mask else 0)
        out.append(str(digit))
    return "".join(out)


def _bbox_quadkeys(bbox4326: list[float], zoom: int) -> list[str]:
    """Every level-``zoom`` quadkey tile covering ``bbox4326`` (the OBM file granularity)."""
    minx, miny, maxx, maxy = [float(v) for v in bbox4326]
    x0, y0 = _lonlat_to_tile(minx, maxy, zoom)  # NW corner -> smallest (x, y)
    x1, y1 = _lonlat_to_tile(maxx, miny, zoom)  # SE corner -> largest (x, y)
    seen: set[str] = set()
    out: list[str] = []
    for x in range(min(x0, x1), max(x0, x1) + 1):
        for y in range(min(y0, y1), max(y0, y1) + 1):
            qk = _tile_to_quadkey(x, y, zoom)
            if qk not in seen:
                seen.add(qk)
                out.append(qk)
    return out


def _building_access(entry: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve OBM access (URL template, quadkey zoom, GeoParquet column names) for the
    buildings factor — each value taken from the manifest ``entry`` when present, else the
    matching INGESTION config default.

    This is what makes the building source *manifest-driven*: a manifest can point A2 at a
    different quadkey-tiled building dataset (or override its columns) with no code/config
    change, while an entry carrying only the on-switch still works off the config defaults.
    Idempotent — passing an already-resolved access dict back in returns the same values.
    """
    entry = entry or {}
    template = entry.get("url_template")
    if not template:
        # accept a template smuggled in as an href (the offline / back-compat path passes the
        # template string straight through), recognised by its ``{quadkey}`` placeholder.
        for h in entry.get("hrefs") or []:
            if "{quadkey}" in str(h):
                template = h
                break
    zoom = entry.get("quadkey_zoom")
    return {
        "url_template": str(template or INGESTION.building_source_url),
        "quadkey_zoom": int(zoom) if zoom is not None else INGESTION.building_quadkey_zoom,
        "geom_col": entry.get("geom_col") or INGESTION.building_geom_col,
        "height_col": entry.get("height_col") or INGESTION.building_height_col,
        "bbox_col": entry.get("bbox_col") or INGESTION.building_bbox_col,
    }


def _building_source_urls(
    bbox4326: list[float], url_template: str | None = None, zoom: int | None = None
) -> list[str]:
    """OBM GeoParquet URL(s) for the quadkey file(s) covering the tile bbox.

    ``url_template``/``zoom`` come from the manifest (via :func:`_building_access`) so the
    source is manifest-driven; both fall back to the INGESTION config default when omitted.
    """
    template = url_template or INGESTION.building_source_url
    zoom = INGESTION.building_quadkey_zoom if zoom is None else int(zoom)
    return [template.format(quadkey=qk) for qk in _bbox_quadkeys(bbox4326, zoom)]


def _parse_gem_height(raw: Any) -> float | None:
    """GEM-taxonomy ``height`` string -> metres (or None when unusable).

    OBM encodes height as e.g. ``"HHT:99.95"`` (explicit metres — strongest), ``"H:2"`` /
    ``"HAPP:2"`` (storey count), ``"HBET:1-3"`` (storey range), compound parts joined by ``+``
    (e.g. ``"H:1+HHT:3.50"``). Explicit metres win; otherwise storeys * meters_per_level. An
    ``HBET`` range collapses to its midpoint (avoids over-stating the tall end). Result is
    clamped to a plausibility range.
    """
    if not raw:
        return None
    metres: float | None = None
    storeys: float | None = None
    for tok in str(raw).split("+"):
        tag, sep, val = tok.strip().partition(":")
        if not sep:
            continue
        tag, val = tag.strip().upper(), val.strip()
        try:
            if tag == "HHT":
                metres = float(val)
            elif tag in ("H", "HAPP"):
                storeys = float(val)
            elif tag == "HBET":
                lo, _, hi = val.partition("-")
                storeys = (float(lo) + float(hi)) / 2.0 if hi else float(lo)
        except ValueError:
            continue
    if metres is None and storeys is not None:
        metres = storeys * INGESTION.meters_per_level
    if metres is None:
        return None
    lo, hi = INGESTION.building_height_clamp
    return float(min(max(metres, lo), hi))


def _read_obm_features(
    urls: list[str],
    bbox4326: list[float],
    geom_col: str | None = None,
    height_col: str | None = None,
    bbox_col: str | None = None,
) -> list[tuple[Any, Any]]:
    """Read OBM building polygons + GEM height string within ``bbox4326`` from ``urls``.

    Anonymous HTTPS GeoParquet via DuckDB (``spatial`` + ``httpfs``), pre-filtered on the
    parquet's own ``bbox`` struct so only intersecting rows decode. Returns ``(wkb, height)``
    per building. The geometry/height/bbox column names default to the OBM schema (INGESTION
    config) but may be overridden by the manifest so a differently-schema'd quadkey-parquet
    source still reads. Isolated + network-only so tests monkeypatch it with synthetic WKB
    (mirrors how ``_read_window_reprojected`` is stubbed). A missing quadkey file (ocean/edge)
    or a transient remote error skips that granule — buildings are opportunistic, never required.
    """
    import warnings

    import duckdb

    minx, miny, maxx, maxy = [float(v) for v in bbox4326]
    gc = geom_col or INGESTION.building_geom_col
    hc = height_col or INGESTION.building_height_col
    bc = bbox_col or INGESTION.building_bbox_col
    con = duckdb.connect()
    con.execute(
        "INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';"
    )
    rows: list[tuple[Any, Any]] = []
    for url in urls:
        try:
            rows.extend(
                con.execute(
                    f"SELECT ST_AsWKB({gc}) AS wkb, {hc} AS h FROM read_parquet('{url}') "
                    f"WHERE {bc}.xmin <= {maxx} AND {bc}.xmax >= {minx} "
                    f"AND {bc}.ymin <= {maxy} AND {bc}.ymax >= {miny} AND {hc} IS NOT NULL"
                ).fetchall()
            )
        except Exception as exc:  # missing granule / transient remote error -> skip
            warnings.warn(f"OBM read skipped for {url}: {exc}")
    con.close()
    return rows


def _rasterize_building_window(
    bbox4326: list[float],
    dst_crs: str,
    dst_gsd_m: float,
    urls: list[str] | None = None,
    access: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rasterize OBM building heights onto the tile's destination grid.

    Same return shape as :func:`_read_window_reprojected` (``{array, transform, crs, nodata,
    gsd_m, shape}``) so it drops straight into the surface composite. Footprints are reprojected
    to ``dst_crs`` and burned (tallest-wins on overlap) onto the :func:`_dst_grid` grid; pixels
    with no building stay nodata (contribute 0 height — a conservative *clear* lean, matching how
    absent canopy is treated). ``access`` (the manifest-resolved URL template / quadkey zoom /
    column names) drives the source; it defaults to the INGESTION config when omitted.
    """
    import numpy as np
    from rasterio.features import rasterize
    from rasterio.warp import transform_geom
    from shapely import wkb as shapely_wkb
    from shapely.geometry import mapping

    nodata = -9999.0
    dst_transform, width, height, _ = _dst_grid(bbox4326, dst_crs, dst_gsd_m)
    access = _building_access(access)
    if urls is None:
        urls = _building_source_urls(bbox4326, access["url_template"], access["quadkey_zoom"])
    rows = _read_obm_features(
        urls,
        bbox4326,
        geom_col=access["geom_col"],
        height_col=access["height_col"],
        bbox_col=access["bbox_col"],
    )

    shapes: list[tuple[Any, float]] = []
    for wkb_bytes, hstr in rows:
        h = _parse_gem_height(hstr)
        if h is None or h <= 0.0:
            continue
        try:
            geom = shapely_wkb.loads(bytes(wkb_bytes))
        except Exception:
            continue
        if geom.is_empty:
            continue
        shapes.append((transform_geom("EPSG:4326", dst_crs, mapping(geom)), float(h)))

    arr = np.full((height, width), nodata, dtype="float32")
    if shapes:
        # rasterio has no MAX merge; burn shortest-first so the tallest building wins each pixel.
        shapes.sort(key=lambda s: s[1])
        rasterize(shapes, out=arr, transform=dst_transform)
    return {
        "array": arr,
        "transform": dst_transform,
        "crs": str(dst_crs),
        "nodata": nodata,
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

    NOTE: the live surface build goes through :func:`_composite_surface`, which fuses
    DEM + max(canopy, building) (buildings from OpenBuildingMap). This canopy-only helper is
    retained for the simple two-layer case and as a reference; the building term is wired in
    the composite, not here.
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


def _composite_surface(
    layers: dict[str, Any], nodata: float
) -> tuple[Any, Any]:
    """Best-available-per-pixel elevation + a per-pixel provenance band.

    Builds one surface by layering the aligned reads, lowest-trust first so higher-trust data
    overwrites it (architecture §5, surface fallback as a *mosaic* not a whole-tile choice):

        bare DEM (code 1)  →  DEM + max(canopy, building, 0) (codes 2/4)  →  lidar DSM (code 3)

    The modelled-fill step fuses DEM + max(canopy, building) where either obstruction layer is
    present; the pixel is tagged code 4 when a building set the height (structure-influenced,
    auditable) and code 2 when canopy did. DEM is globally complete, so the result has full
    coverage even where the lidar is patchy — which is what stops a partial lidar tile from
    leaving ~half its points ``undetermined``. Each input is ``{"array", "nodata"}`` (or absent).
    Returns ``(elev, provenance)`` aligned to the same grid; ``provenance`` is 0 where no source
    had a value (genuinely no datum).
    """
    import numpy as np

    dem = layers.get("terrain")
    lidar = layers.get("surface")
    canopy = layers.get("canopy")
    building = layers.get("buildings")
    ref = dem or lidar or canopy or building
    shape = np.asarray(ref["array"]).shape
    elev = np.full(shape, nodata, dtype="float32")
    prov = np.zeros(shape, dtype="int16")

    def _valid(layer: Any) -> tuple[Any, Any]:
        arr = np.asarray(layer["array"], dtype="float32")
        nd = float(layer.get("nodata", nodata))
        return arr, np.isfinite(arr) & (arr != nd)

    def _height(layer: Any) -> tuple[Any, Any]:
        """Non-negative obstruction height + validity mask for a layer (zeros when absent)."""
        if layer is None:
            return np.zeros(shape, dtype="float32"), np.zeros(shape, dtype=bool)
        arr, ok = _valid(layer)
        return np.where(ok, np.maximum(arr, 0.0), 0.0), ok

    # 1) bare DEM (lowest trust, but complete coverage)
    if dem is not None:
        dem_arr, dem_ok = _valid(dem)
        elev = np.where(dem_ok, dem_arr, elev)
        prov = np.where(dem_ok, 1, prov)
        # 2/4) modelled surface: DEM + max(canopy, building) where either obstruction is present.
        if canopy is not None or building is not None:
            can_h, can_ok = _height(canopy)
            bld_h, bld_ok = _height(building)
            have_obs = dem_ok & (can_ok | bld_ok)
            elev = np.where(have_obs, dem_arr + np.maximum(can_h, bld_h), elev)
            prov = np.where(have_obs, 2, prov)
            # code 4 where a building set the height (building present and >= canopy).
            prov = np.where(have_obs & bld_ok & (bld_h >= can_h), 4, prov)
    # 3) lidar DSM overrides wherever it has a real value (already carries canopy + buildings)
    if lidar is not None:
        li_arr, li_ok = _valid(lidar)
        elev = np.where(li_ok, li_arr, elev)
        prov = np.where(li_ok, 3, prov)

    return elev.astype("float32"), prov


def _provenance_fractions(prov: Any) -> dict[str, float]:
    """Share of pixels from each source (for the sidecar / QA) — counts only real datum pixels.

    ``building`` is the modelled-fill pixels where a building set the height (code 4); ``pseudo``
    is canopy-set modelled fill (code 2). Both are DEM+obstruction; the split makes the building
    contribution auditable.
    """
    import numpy as np

    prov = np.asarray(prov)
    total = int((prov > 0).sum())
    if total == 0:
        return {"lidar": 0.0, "pseudo": 0.0, "building": 0.0, "dem_fill": 0.0}
    return {
        "lidar": round(float((prov == 3).sum()) / total, 4),
        "pseudo": round(float((prov == 2).sum()) / total, 4),
        "building": round(float((prov == 4).sum()) / total, 4),
        "dem_fill": round(float((prov == 1).sum()) / total, 4),
    }


def _valid_fraction(array: Any, nodata: float) -> float:
    """Share of finite, non-nodata pixels — feeds coverage_flag / confidence."""
    import numpy as np

    arr = np.asarray(array)
    if arr.size == 0:
        return 0.0
    mask = np.isfinite(arr) & (arr != nodata)
    return float(mask.mean())


def _write_cog(
    path: Path,
    array: Any,
    transform: Any,
    crs: str,
    nodata: float,
    provenance: Any = None,
) -> None:
    """Write a tiled, deflate-compressed float32 GeoTIFF surface. Band 1 is elevation; when
    ``provenance`` is given it is written as band 2 (per-pixel source code) so the analysis
    sampler can read what data each point sat on without a second file."""
    import rasterio

    path.parent.mkdir(parents=True, exist_ok=True)
    count = 1 if provenance is None else 2
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        dtype="float32",
        count=count,
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
        if provenance is not None:
            dst.write(provenance.astype("float32"), 2)
            dst.set_band_description(2, "provenance")


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
    # Tile-level summary only; the authoritative signal is the per-pixel provenance band, which
    # A3 samples per point (a mosaic is mostly-complete DEM with lidar where available).
    base = {"mosaic": "high", "true_dsm": "high", "pseudo_dsm": "medium"}.get(surface_mode, "low")
    if coverage != "ok":
        base = {"high": "medium", "medium": "low"}.get(base, "low")
    return coverage, base


@tool(
    "fetch_aligned_surface",
    "Build ONE aligned elevation surface for a tile from the H1-approved datasets: read "
    "the COG window(s) covering the tile bbox, reproject to a metric CRS, and either pass "
    "through a true lidar DSM or fuse DEM + max(canopy, building) into a pseudo-DSM/mosaic. "
    "Buildings (OpenBuildingMap) fuse opportunistically into the mosaic/pseudo_dsm fill where "
    "the manifest carries a 'buildings' factor; that entry may be a {url_template (with a "
    "{quadkey} placeholder), quadkey_zoom, geom_col, height_col, bbox_col, vintage} object to "
    "point at any quadkey-tiled building source (omitted keys fall back to config). Deterministic, "
    "cached and idempotent (re-running a tile with unchanged inputs is a no-op). You do "
    "NOT score risk here. `tile_id` names the cache entry; `bbox` is "
    "[min_lon,min_lat,max_lon,max_lat] in EPSG:4326; `manifest` maps obstruction factor -> "
    "asset href (keys: surface/dsm, terrain/dem, canopy, buildings; values are hrefs or "
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
    buildings = layers.get("buildings")

    # Auto-pick the best mode the manifest can actually support, if not told one. ``mosaic`` is
    # the new best: lidar over a DEM base so coverage is complete (no partial-lidar holes →
    # no spurious ``undetermined`` downstream). The single-source modes remain for when the
    # manifest carries only one of the two.
    if requested_mode:
        surface_mode = requested_mode
    elif dsm and dem:
        surface_mode = "mosaic"
    elif dsm:
        surface_mode = "true_dsm"
    elif dem and (canopy or buildings):
        surface_mode = "pseudo_dsm"
    elif dem:
        surface_mode = "cover_proxy"
    else:
        return _error(
            "manifest has none of: a surface/dsm href, or a terrain/dem href to build "
            "from — cannot ingest this tile"
        )

    # Confirm the chosen mode has its required layers; if not, point at the next fallback.
    # pseudo_dsm needs terrain + at least ONE obstruction layer (canopy and/or building); the
    # obstruction layers fuse opportunistically (read below), so only terrain is hard-required.
    need = {
        "mosaic": ["surface", "terrain"],
        "true_dsm": ["surface"],
        "pseudo_dsm": ["terrain"],
        "cover_proxy": ["terrain"],
    }[surface_mode]
    if surface_mode == "pseudo_dsm" and not (canopy or buildings):
        nxt = _next_surface_mode(surface_mode)
        hint = f" try surface_mode={nxt!r}" if nxt else ""
        return _error(
            "surface_mode='pseudo_dsm' needs a canopy and/or buildings layer to fuse onto the "
            f"DEM, not provided.{hint}"
        )
    missing = [f for f in need if f not in layers]
    if missing:
        nxt = _next_surface_mode(surface_mode)
        hint = f" try surface_mode={nxt!r}" if nxt else ""
        return _error(
            f"surface_mode={surface_mode!r} needs manifest factor(s) {missing}, "
            f"not provided.{hint}"
        )

    target_crs = _resolve_target_crs(args.get("target_crs"), bbox)

    # Buildings (OpenBuildingMap) fuse opportunistically into the modelled-fill of the mosaic and
    # pseudo-DSM surfaces — the non-lidar regions where roofs would otherwise be invisible (the
    # lidar DSM already carries them). Resolve the actual quadkey file URL(s) so the cache key
    # tracks the building data read, not just the template marker in the manifest.
    use_buildings = buildings is not None and surface_mode in ("mosaic", "pseudo_dsm")
    # Access (URL template / quadkey zoom / columns) is read from the manifest's buildings
    # entry — config only supplies defaults — so the building source is fully manifest-driven.
    building_access = _building_access(buildings) if use_buildings else None
    building_urls = (
        _building_source_urls(
            bbox, building_access["url_template"], building_access["quadkey_zoom"]
        )
        if use_buildings
        else None
    )

    href_map = {
        "surface": dsm["hrefs"] if dsm else None,
        "terrain": dem["hrefs"] if dem else None,
        "canopy": canopy["hrefs"] if canopy else None,
        "buildings": building_urls,
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

    # Read every layer the mode draws on — the mosaic and pseudo-DSM fill both fuse canopy when
    # the manifest carries it — all onto the same (bbox, crs, gsd)-derived grid so they composite
    # pixel-for-pixel. The raster COG factors go through the windowed-read path; buildings (OBM
    # vector) are rasterized below.
    read_factors = list(need)
    if surface_mode in ("mosaic", "pseudo_dsm") and "canopy" in layers:
        read_factors.append("canopy")
    vintage_map = {f: layers[f].get("vintage") for f in read_factors}
    try:
        reads = {
            f: _read_window_mosaic(
                [_sign_href(h) for h in layers[f]["hrefs"]], bbox, target_crs, target_gsd_m
            )
            for f in read_factors
        }
    except Exception as exc:
        nxt = _next_surface_mode(surface_mode)
        hint = f" Retry with surface_mode={nxt!r}." if nxt else ""
        return _error(
            f"read/reproject failed for surface_mode={surface_mode!r} on tile {tile_id}: "
            f"{exc}.{hint}"
        )

    # Buildings are an opportunistic enhancement: a failed/empty OBM read drops the building term
    # and keeps the tile (the surface is still valid without it), rather than failing the tile.
    if use_buildings:
        try:
            bld = _rasterize_building_window(
                bbox, target_crs, target_gsd_m, urls=building_urls, access=building_access
            )
            if bld is not None and bool((bld["array"] != bld["nodata"]).any()):
                reads["buildings"] = bld
                vintage_map["buildings"] = buildings.get("vintage")
        except Exception as exc:  # OBM remote/parse error -> degrade gracefully
            import warnings

            warnings.warn(f"building layer skipped for tile {tile_id}: {exc}")

    ref = reads.get("terrain") or reads.get("surface") or reads.get("canopy")
    nodata = ref["nodata"]
    transform = ref["transform"]
    dsm_arr, provenance = _composite_surface(reads, nodata)

    valid_frac = _valid_fraction(dsm_arr, nodata)
    coverage_flag, confidence = _coverage_and_confidence(surface_mode, valid_frac)

    dsm_path = Path(str(base) + "_dsm.tif")
    _write_cog(dsm_path, dsm_arr, transform, target_crs, nodata, provenance=provenance)

    payload = {
        "tile_id": tile_id,
        "dsm_uri": str(dsm_path),
        "dem_uri": None,
        "crs": target_crs,
        "gsd_m": target_gsd_m,
        "surface_mode": surface_mode,
        "provenance_band": 2,
        "provenance_fractions": _provenance_fractions(provenance),
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


def _search_item_hrefs(
    collection: str,
    bbox: list[float],
    catalog: str = "planetary_computer",
    max_items: int = 20,
) -> list[str]:
    """Resolve **every** intersecting granule's (UNSIGNED) asset href for a collection+tile.

    ``stac_item_read`` returns the single best item; this returns all of them, finest
    resolution first, so ``fetch_aligned_surface`` can mosaic a tile that spans a granule
    boundary (the deterministic A2 path's granule-mosaic input — see ``leo_pipeline.offline``).
    Hrefs are returned unsigned; signing happens at download time inside the fetch tool.
    """
    catalog_url = DISCOVERY.stac_catalogs.get(catalog, catalog)
    from pystac_client import Client

    client = Client.open(catalog_url)
    items = list(client.search(collections=[collection], bbox=bbox, max_items=max_items).items())

    def _sort_key(it: Any) -> tuple[float, str]:
        summ = _summarize_stac_item(it)
        gsd = summ.get("gsd_m")
        return (gsd if gsd is not None else 1e9, summ.get("datetime") or "")

    hrefs: list[str] = []
    for it in sorted(items, key=_sort_key):
        href = _summarize_stac_item(it).get("asset_href")
        if href and href not in hrefs:
            hrefs.append(href)
    return hrefs


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


# --- A3 analysis tools ----------------------------------------------------------------
# The analysis agent reasons only about *which params to use and how to handle edges*; these
# tools do the deterministic horizon/viewshed math (leo_pipeline.horizon) over the A2 surface
# and never fetch data or browse the web. See docs/architecture.md (A3, §3 compute_sky_
# obstruction / find_clear_sky_spot) and docs/rationale.md (the per-azimuth horizon profile).


def _resolve_sky_spec(args: dict[str, Any]) -> Any:
    """Build a horizon.SkySpec from the version-stamped ANALYSIS config, overridden by the
    per-call ``sky_spec`` block and the top-level azimuth_step/max_radius/earth_curvature
    args. The agent only *narrows* the spec for a call; the defaults are the approved spec."""
    from leo_pipeline.horizon import SkySpec

    s = args.get("sky_spec") or {}
    return SkySpec(
        min_elevation_deg=float(s.get("min_elev_deg", ANALYSIS.min_elevation_deg)),
        az_center_deg=float(s.get("az_center_deg", ANALYSIS.pointing_azimuth_deg)),
        az_halfwidth_deg=float(s.get("az_halfwidth_deg", ANALYSIS.cone_half_angle_deg)),
        az_weighting=str(s.get("az_weighting", ANALYSIS.az_weighting)),
        gso_keepout_halfwidth_deg=float(
            s.get("gso_keepout_halfwidth_deg", ANALYSIS.gso_keepout_halfwidth_deg)
        ),
        band_clear_max_pct=float(s.get("band_clear_max_pct", ANALYSIS.band_clear_max_pct)),
        band_severe_min_pct=float(s.get("band_severe_min_pct", ANALYSIS.band_severe_min_pct)),
        azimuth_step_deg=float(args.get("azimuth_step_deg") or ANALYSIS.azimuth_step_deg),
        max_radius_m=float(args.get("max_radius_m") or ANALYSIS.max_radius_m),
        earth_curvature=bool(
            args.get("earth_curvature")
            if args.get("earth_curvature") is not None
            else ANALYSIS.earth_curvature
        ),
        sigma_h_m=float(s.get("sigma_h_m", ANALYSIS.sigma_h_m)),
        confidence_near_field_m=float(ANALYSIS.confidence_near_field_m),
        conf_near_sampled_high=float(ANALYSIS.conf_near_sampled_high),
        conf_near_sampled_med=float(ANALYSIS.conf_near_sampled_med),
    )


def _surface_confidence_for(dsm_uri: str) -> str | None:
    """Best-effort lookup of the A2 surface's own confidence from its cache sidecar, so a
    degraded/low-confidence surface flows through to a lower analysis confidence."""
    try:
        base = dsm_uri[: -len("_dsm.tif")] if dsm_uri.endswith("_dsm.tif") else dsm_uri
        sidecar = Path(base + ".json")
        if sidecar.exists():
            return json.loads(sidecar.read_text()).get("confidence")
    except Exception:
        pass
    return None


@tool(
    "compute_sky_obstruction",
    "Score sky obstruction for a batch of locations against ONE aligned surface (the A2 "
    "DSM for their tile). For each point it builds the per-azimuth horizon profile, compares "
    "it to the required sky region (elevations above the dish's minimum reception angle over "
    "its azimuth cone), and returns the dwell-time-weighted obstruction percentage the "
    "Starlink app would report, plus a clear/at_risk/severe tier (or 'undetermined' when no "
    "surface sits under the point). Deterministic geospatial math — you do NOT fetch data "
    "here. `points` is a list of {location_id, lat, lon} (EPSG:4326); `dsm_uri` is the cached "
    "surface from A2; `dish_height_m` is the mount height above the surface (default 0, "
    "roof-only as-installed); "
    "`sky_spec` overrides the approved spec (min_elev_deg, az_center_deg, az_halfwidth_deg, "
    "az_weighting in uniform|north_biased); `azimuth_step_deg`/`max_radius_m`/`earth_curvature` "
    "tune the profile. Returns array<{location_id, obstruction_pct, blocked_azimuths, "
    "horizon_profile, risk_tier, confidence}>.",
    {
        "points": list,
        "dsm_uri": str,
        "dish_height_m": float,
        "sky_spec": dict,
        "azimuth_step_deg": float,
        "max_radius_m": float,
        "earth_curvature": bool,
    },
)
async def compute_sky_obstruction(args: dict[str, Any]) -> dict[str, Any]:
    points = args.get("points")
    dsm_uri = (args.get("dsm_uri") or "").strip()
    dish_height_m = args.get("dish_height_m")
    dish_height_m = (
        ANALYSIS.mount_heights_m[0] if dish_height_m is None else float(dish_height_m)
    )

    if not isinstance(points, list) or not points:
        return _error("points must be a non-empty list of {location_id, lat, lon}")
    if not dsm_uri:
        return _error("dsm_uri is required (the A2 surface for these points' tile)")
    if not Path(dsm_uri).exists():
        return _error(f"dsm_uri not found: {dsm_uri} — has A2 built this tile's surface?")

    from leo_pipeline import horizon

    spec = _resolve_sky_spec(args)
    surface_conf = _surface_confidence_for(dsm_uri)
    try:
        sampler = horizon._cached_sampler(dsm_uri)
    except Exception as exc:
        return _error(f"could not open surface {dsm_uri}: {exc}")

    results: list[dict[str, Any]] = []
    for p in points:
        if not isinstance(p, dict) or p.get("lat") is None or p.get("lon") is None:
            return _error(f"each point needs lat+lon: {p!r}")
        try:
            score = horizon.score_point(
                sampler, float(p["lon"]), float(p["lat"]), dish_height_m, spec
            )
        except Exception as exc:
            return _error(f"scoring failed for {p.get('location_id')!r}: {exc}")
        conf = horizon.confidence_for_score(score, spec, surface_confidence=surface_conf)
        results.append(
            {
                "location_id": p.get("location_id"),
                "obstruction_pct": score["obstruction_pct"],
                "blocked_azimuths": score["blocked_azimuths"],
                "horizon_profile": score["horizon_profile"],
                "risk_tier": score["risk_tier"],
                "confidence": conf,
                "surface_provenance": score.get("surface_provenance"),
                "dish_height_m": score["dish_height_m"],
            }
        )

    tiers: dict[str, int] = {}
    for r in results:
        tiers[r["risk_tier"]] = tiers.get(r["risk_tier"], 0) + 1
    return _text(
        {
            "dsm_uri": dsm_uri,
            "spec_version": ANALYSIS.spec_version,
            "n_points": len(results),
            "tier_counts": tiers,
            "results": results,
        }
    )


@tool(
    "find_clear_sky_spot",
    "Scenario 3 helper: within an X-metre buffer of a flagged location, search a grid of "
    "candidate positions and mount heights for ones with lower sky obstruction — i.e. 'move "
    "the dish here' or 'raise it to Y m'. Reuses the same horizon engine as "
    "compute_sky_obstruction. Candidates are clipped to the buffer (a parcel-clip proxy — "
    "cross-parcel suggestions are out of scope). `lat`/`lon` is the location (EPSG:4326); "
    "`dsm_uri` is the A2 surface; `buffer_m` is the search radius (default 50); `sky_spec` "
    "overrides the approved spec; `candidate_grid_m` is the grid spacing (default 5); "
    "`dish_height_candidates_m` are the mount heights to try (default [0, 0.3, 0.6] — roof-only "
    "plus the Starlink 1–2 ft poles). Returns the "
    "current-position baseline plus array<{lat, lon, dish_height_m, obstruction_pct, "
    "improvement_pct}> sorted best-first.",
    {
        "lat": float,
        "lon": float,
        "buffer_m": float,
        "dsm_uri": str,
        "sky_spec": dict,
        "candidate_grid_m": float,
        "dish_height_candidates_m": list,
    },
)
async def find_clear_sky_spot(args: dict[str, Any]) -> dict[str, Any]:
    lat = args.get("lat")
    lon = args.get("lon")
    dsm_uri = (args.get("dsm_uri") or "").strip()
    buffer_m = float(args.get("buffer_m") or 50.0)
    grid_m = max(1.0, float(args.get("candidate_grid_m") or 5.0))
    heights = args.get("dish_height_candidates_m") or list(ANALYSIS.mount_heights_m)

    if lat is None or lon is None:
        return _error("lat and lon are required")
    if not dsm_uri:
        return _error("dsm_uri is required (the A2 surface for this location's tile)")
    if not Path(dsm_uri).exists():
        return _error(f"dsm_uri not found: {dsm_uri} — has A2 built this tile's surface?")
    try:
        heights = [float(h) for h in heights]
    except (TypeError, ValueError):
        return _error("dish_height_candidates_m must be a list of numbers")

    import numpy as np

    from leo_pipeline import horizon

    spec = _resolve_sky_spec(args)
    surface_conf = _surface_confidence_for(dsm_uri)
    try:
        sampler = horizon._cached_sampler(dsm_uri)
    except Exception as exc:
        return _error(f"could not open surface {dsm_uri}: {exc}")

    x0, y0 = sampler.lonlat_to_xy(float(lon), float(lat))
    # Baseline = current position at the lowest candidate mount height.
    base_h = min(heights)
    base = horizon.score_xy(sampler, x0, y0, base_h, spec)
    base_pct = base["obstruction_pct"]

    # Grid of offsets within the buffer circle (metric), including the centre (offset 0).
    steps = np.arange(-buffer_m, buffer_m + grid_m, grid_m)
    candidates: list[dict[str, Any]] = []
    for dx in steps:
        for dy in steps:
            if dx * dx + dy * dy > buffer_m * buffer_m:
                continue
            for h in heights:
                score = horizon.score_xy(sampler, x0 + float(dx), y0 + float(dy), h, spec)
                pct = score["obstruction_pct"]
                if pct is None:  # undetermined candidate (off-surface) — skip
                    continue
                clon, clat = sampler.xy_to_lonlat(x0 + float(dx), y0 + float(dy))
                improvement = (
                    round(base_pct - pct, 2) if base_pct is not None else None
                )
                candidates.append(
                    {
                        "lat": round(clat, 7),
                        "lon": round(clon, 7),
                        "dish_height_m": h,
                        "obstruction_pct": pct,
                        "improvement_pct": improvement,
                        "risk_tier": score["risk_tier"],
                        "confidence": horizon.confidence_for_score(
                            score, spec, surface_confidence=surface_conf
                        ),
                    }
                )

    candidates.sort(key=lambda c: (c["obstruction_pct"], c["dish_height_m"]))
    return _text(
        {
            "dsm_uri": dsm_uri,
            "spec_version": ANALYSIS.spec_version,
            "baseline": {
                "lat": round(float(lat), 7),
                "lon": round(float(lon), 7),
                "dish_height_m": base_h,
                "obstruction_pct": base_pct,
                "risk_tier": base["risk_tier"],
            },
            "n_candidates": len(candidates),
            "candidates": candidates[:10],
        }
    )


# --- A4 validation / QA tools ---------------------------------------------------------
# The QA agent reasons only about *triage* (explain a flagged anomaly, decide if it routes to
# the H2 human gate); these tools do the deterministic stats + anomaly rules over the input
# table and the A3 findings (leo_pipeline.qa) and never mutate run state or publish anything.
# See docs/architecture.md (A4, §5 failure handling, §7 H2 gate).


def _resolve_aoi(arg: Any) -> list[float]:
    """AOI bbox for the input audit: an explicit arg, else the A1 manifest's aoi_bbox,
    else the QA spec default (the confirmed AOI)."""
    if isinstance(arg, (list, tuple)) and len(arg) == 4:
        return [float(x) for x in arg]
    try:
        manifest = json.loads(DISCOVERY.manifest_path.read_text())
        bbox = manifest.get("aoi_bbox")
        if isinstance(bbox, list) and len(bbox) == 4:
            return [float(x) for x in bbox]
    except Exception:
        pass
    return list(QA.aoi_bbox)


def _load_findings(
    findings_arg: Any, findings_dir: Path, tile: str | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve the A3 findings to QA: an inline ``findings`` list, else the per-tile JSON
    files A3 wrote to ``findings_dir`` (optionally just one ``tile``). Returns
    (findings, source_files)."""
    if isinstance(findings_arg, list) and findings_arg:
        return [f for f in findings_arg if isinstance(f, dict)], ["<inline>"]
    if not findings_dir.exists():
        return [], []
    pattern = f"findings_{tile}.json" if tile else "findings_*.json"
    findings: list[dict[str, Any]] = []
    sources: list[str] = []
    for path in sorted(findings_dir.glob(pattern)):
        try:
            rows = json.loads(path.read_text())
        except Exception:
            continue
        if isinstance(rows, list):
            findings.extend(r for r in rows if isinstance(r, dict))
            sources.append(path.name)
    return findings, sources


def _coord_id(location_id: Any) -> int | None:
    """Parse the ``coord_<id>`` work-item id A3 stamps back to its integer coord_id."""
    s = str(location_id or "")
    if s.startswith("coord_"):
        try:
            return int(s[len("coord_") :])
        except ValueError:
            return None
    return None


def _county_weight_maps(
    findings: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, float]]:
    """Best-effort {location_id -> county FIPS} and {location_id -> n_locations weight} for
    the findings, joined through the dedup maps + the locations CSV. The county FIPS is the
    first 5 digits of the 15-digit ``geoid_cb`` census block (state[2] + county[3]).

    Enables the county grouping and household-weighted regional rates when the
    ``unique_coords``/``location_coord_map`` Parquet and the CSV are on disk; returns empty
    maps (tile-only grouping, unit weights) when they're absent or unreadable. Restricted to
    the coord_ids actually in the findings, so QA over a few tiles never scans all 4.5M coords.
    """
    coord_ids = {cid for f in findings if (cid := _coord_id(f.get("location_id"))) is not None}
    if not coord_ids:
        return {}, {}
    interim = ingest.PATHS.data_interim
    unique_path = interim / ingest.UNIQUE_COORDS_FILE
    map_path = interim / ingest.LOCATION_MAP_FILE
    csv_path = ingest.PATHS.locations_csv
    if not (unique_path.exists() and map_path.exists() and csv_path.exists()):
        return {}, {}
    try:
        con = ingest.connect()  # CSV exposed as `locations`
    except FileNotFoundError:
        return {}, {}
    try:
        id_list = ",".join(str(c) for c in sorted(coord_ids))
        uq = str(unique_path).replace("'", "''")
        mp = str(map_path).replace("'", "''")
        rows = con.execute(
            f"""
            WITH uq AS (
                SELECT coord_id, n_locations FROM read_parquet('{uq}')
                WHERE coord_id IN ({id_list})
            ),
            cmap AS (SELECT location_id, coord_id FROM read_parquet('{mp}'))
            -- geoid_cb is a 15-digit census BLOCK; the first 5 digits are the county FIPS
            -- (state[2] + county[3]) — the meaningful "county at 100% at-risk" grouping unit.
            SELECT uq.coord_id, uq.n_locations,
                   substr(CAST(min(l.geoid_cb) AS VARCHAR), 1, 5) AS county
            FROM uq
            JOIN cmap USING (coord_id)
            JOIN locations l ON l.location_id = cmap.location_id
            GROUP BY uq.coord_id, uq.n_locations
            """
        ).fetchall()
    except Exception:
        return {}, {}
    finally:
        con.close()
    county_of: dict[str, str] = {}
    weight_of: dict[str, float] = {}
    for coord_id, n_locations, county in rows:
        lid = f"coord_{int(coord_id)}"
        weight_of[lid] = float(n_locations or 1)
        if county is not None:
            county_of[lid] = str(county)
    return county_of, weight_of


@tool(
    "qa_input_audit",
    "Audit the raw locations table for quarantine-worthy input rows and summarise each "
    "bucket (architecture §5 'bad input row'): null / out-of-range coordinates, the (0,0) "
    "null island, points outside the AOI, and lat/lon-swapped rows. Read-only and counts "
    "only (the ~4.7M-row table is never materialised) — de-dup itself is the deterministic "
    "ingest pre-step; this re-audits and sizes the quarantine queue. `aoi_bbox` is an "
    "optional [min_lon,min_lat,max_lon,max_lat] override (defaults to the A1 manifest's AOI, "
    "else the QA spec default). Returns {total_rows, distinct_coords, quarantine_total, "
    "quarantine_rate, swapped_suspect, issues:[{kind,count,detail}]}.",
    {"aoi_bbox": list},
)
async def qa_input_audit(args: dict[str, Any]) -> dict[str, Any]:
    aoi = _resolve_aoi(args.get("aoi_bbox"))
    try:
        con = ingest.connect()
    except FileNotFoundError as exc:
        return _error(f"locations CSV not found for the input audit: {exc}")
    try:
        counts = qa_engine.input_quality_counts(con, aoi)
    except Exception as exc:
        return _error(str(exc))
    finally:
        con.close()
    report = qa_engine.input_quality_report(counts, aoi)
    report["aoi_bbox"] = aoi
    report["qa_spec_version"] = QA.qa_spec_version
    return _text(report)


@tool(
    "qa_location_batch",
    "Scan a batch of A3 obstruction findings for output anomalies (architecture §5) and "
    "return a QA report for the H2 gate. Deterministic stats + rules — you do NOT re-score "
    "here. Rules: a region (tile, and county when the dedup maps are on disk) implausibly "
    "saturated with at-risk locations; a risk_tier that disagrees with its own "
    "obstruction_pct under the spec bands; too many undetermined / low-confidence scores; a "
    "degenerate all-identical distribution; mixed spec_version. Pass findings inline via "
    "`findings` (a list of A3 result dicts), or omit it to load the per-tile findings A3 "
    "wrote to data/interim/analysis (optionally just one `tile`). Returns {qa_spec_version, "
    "summary, grouped_by, n_anomalies, review_required, has_critical, "
    "anomalies:[{rule,severity,scope,detail,metric,threshold,sample_ids}]}.",
    {"findings": list, "tile": str, "findings_dir": str},
)
async def qa_location_batch(args: dict[str, Any]) -> dict[str, Any]:
    findings_dir = (
        Path(args["findings_dir"]).expanduser()
        if args.get("findings_dir")
        else ANALYSIS.findings_dir
    )
    tile = (args.get("tile") or "").strip() or None
    findings, sources = _load_findings(args.get("findings"), findings_dir, tile)
    if not findings:
        return _error(
            "no findings to QA — pass `findings` inline, or run the analysis first "
            f"(python -m leo_pipeline.run_analysis --compute); looked in {findings_dir}"
        )
    county_of, weight_of = _county_weight_maps(findings)
    report = qa_engine.run_output_qa(
        findings, county_of=county_of or None, weight_of=weight_of or None
    )
    report["n_findings"] = len(findings)
    report["sources"] = sources
    if not county_of:
        report.setdefault("notes", []).append(
            "county grouping skipped (dedup maps / locations CSV not on disk); tile grouping only"
        )
    return _text(report)


# --- A5 reporting / insight tools -----------------------------------------------------
# The reporting agent reasons only about the *narrative* (write the officer-facing summary);
# these tools do the deterministic aggregation + map-tile build (leo_pipeline.report) and a
# scoped write into outputs/. They never re-score or touch the open web. See docs/
# architecture.md (A5, §7 H2 insight sign-off).


def _coord_lonlat_map(
    findings: list[dict[str, Any]],
) -> dict[str, tuple[float, float]]:
    """Best-effort {location_id -> (lon, lat)} for the findings, read from the unique-
    coordinate work list, so A5 can place each finding on the map. Restricted to the
    coord_ids in the findings; returns {} when the work list is absent or unreadable (the
    map then falls back to any lon/lat already on the findings)."""
    coord_ids = {cid for f in findings if (cid := _coord_id(f.get("location_id"))) is not None}
    if not coord_ids:
        return {}
    unique_path = ingest.PATHS.data_interim / ingest.UNIQUE_COORDS_FILE
    if not unique_path.exists():
        return {}
    try:
        import duckdb

        con = duckdb.connect()
        id_list = ",".join(str(c) for c in sorted(coord_ids))
        uq = str(unique_path).replace("'", "''")
        rows = con.execute(
            f"SELECT coord_id, longitude, latitude FROM read_parquet('{uq}') "
            f"WHERE coord_id IN ({id_list})"
        ).fetchall()
        con.close()
    except Exception:
        return {}
    return {f"coord_{int(c)}": (float(lon), float(lat)) for c, lon, lat in rows}


@tool(
    "aggregate_findings",
    "Roll the A3 obstruction findings up to county + state (household-weighted by the "
    "n_locations behind each unique coordinate), ranked by at-risk households — the "
    "prioritised summary a broadband officer acts on. Deterministic; you do NOT re-score. "
    "Pass findings inline via `findings`, or omit it to load the per-tile findings A3 wrote "
    "to data/interim/analysis (optionally just one `tile`). County grouping needs the dedup "
    "maps + locations CSV on disk; without them only run totals are returned. Returns "
    "{report_version, summary, grouped_by, counties:[...], states:[...]} (counties/states "
    "carry households, tier_households, at_risk_households, at_risk_rate).",
    {"findings": list, "tile": str, "findings_dir": str},
)
async def aggregate_findings(args: dict[str, Any]) -> dict[str, Any]:
    findings_dir = (
        Path(args["findings_dir"]).expanduser() if args.get("findings_dir")
        else ANALYSIS.findings_dir
    )
    tile = (args.get("tile") or "").strip() or None
    findings, sources = _load_findings(args.get("findings"), findings_dir, tile)
    if not findings:
        return _error(
            "no findings to aggregate — pass `findings` inline, or run the analysis first "
            f"(python -m leo_pipeline.run_analysis --compute); looked in {findings_dir}"
        )
    county_of, weight_of = _county_weight_maps(findings)
    agg = report_engine.aggregate_findings(
        findings, county_of=county_of or None, weight_of=weight_of or None
    )
    agg["n_findings"] = len(findings)
    agg["sources"] = sources
    if not county_of:
        agg.setdefault("notes", []).append(
            "county/state grouping skipped (dedup maps / CSV not on disk); run totals only"
        )
    return _text(agg)


@tool(
    "render_map",
    "Build the interactive risk map from the A3 findings: a per-location GeoJSON point layer "
    "coloured by risk tier, tiled to PMTiles with tippecanoe (scales to ~1M points, no "
    "server), plus a self-contained MapLibre HTML viewer. Deterministic; you do NOT re-score. "
    "Pass findings inline via `findings`, or omit it to load A3's per-tile findings "
    "(optionally one `tile`). `run_tiles` (default true) toggles the tippecanoe step — if the "
    "binary is missing the GeoJSON + HTML are still written and the note says so. Writes into "
    "outputs/. Returns {n_features, geojson, html, pmtiles?, center, zoom, note}.",
    {"findings": list, "tile": str, "findings_dir": str, "run_tiles": bool},
)
async def render_map(args: dict[str, Any]) -> dict[str, Any]:
    findings_dir = (
        Path(args["findings_dir"]).expanduser() if args.get("findings_dir")
        else ANALYSIS.findings_dir
    )
    tile = (args.get("tile") or "").strip() or None
    findings, _ = _load_findings(args.get("findings"), findings_dir, tile)
    if not findings:
        return _error(
            "no findings to map — pass `findings` inline, or run the analysis first "
            f"(python -m leo_pipeline.run_analysis --compute); looked in {findings_dir}"
        )
    run_tiles = args.get("run_tiles")
    run_tiles = True if run_tiles is None else bool(run_tiles)
    lonlat_of = _coord_lonlat_map(findings)
    map_info = report_engine.build_map(
        findings, REPORTING.outputs_dir, lonlat_of=lonlat_of or None, run_tiles=run_tiles
    )
    if map_info["n_features"] == 0:
        map_info["warning"] = (
            "0 mappable features — findings carried no lon/lat and no unique_coords work list "
            "was on disk to resolve them"
        )
    return _text(map_info)


# Extensions write_report will persist (officer report + its data sidecar); anything else is
# rejected so the agent cannot write executable or arbitrary files through this surface.
_REPORT_EXTS = (".md", ".json", ".txt", ".html")


@tool(
    "write_report",
    "Persist the officer-facing report you composed into the outputs/ directory (the A5 "
    "deliverable for the H2 sign-off). `content` is the full text (typically the markdown "
    "decision log); `filename` is the basename to write (default 'decision_log.md'; must end "
    ".md/.json/.txt/.html — no paths or traversal). Use this for the narrative you write; use "
    "aggregate_findings / render_map for the numbers and the map. Returns {path, bytes}.",
    {"content": str, "filename": str},
)
async def write_report(args: dict[str, Any]) -> dict[str, Any]:
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        return _error("content must be a non-empty string")
    filename = (args.get("filename") or "decision_log.md").strip()
    # Basename only — reject any directory component or traversal.
    if filename != Path(filename).name or filename in ("", ".", ".."):
        return _error(f"filename must be a bare basename, got {filename!r}")
    if not filename.lower().endswith(_REPORT_EXTS):
        return _error(f"filename must end with one of {list(_REPORT_EXTS)}")
    out_dir = REPORTING.outputs_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(content)
    return _text({"path": str(path), "bytes": len(content.encode("utf-8"))})


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
        compute_sky_obstruction,
        find_clear_sky_spot,
        qa_input_audit,
        qa_location_batch,
        aggregate_findings,
        render_map,
        write_report,
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
    "mcp__leo__compute_sky_obstruction",
    "mcp__leo__find_clear_sky_spot",
    "mcp__leo__qa_input_audit",
    "mcp__leo__qa_location_batch",
    "mcp__leo__aggregate_findings",
    "mcp__leo__render_map",
    "mcp__leo__write_report",
]
