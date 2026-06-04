"""Deterministic reporting / insight engine for the A5 agent.

A5's three officer-facing deliverables (architecture.md §2 A5 node, §7 H2 sign-off) are all
built here, deterministically, so the LLM only writes the narrative — it never computes a
number or hand-rolls a map:

- **Aggregates** — ``aggregate_findings`` rolls the A3 obstruction findings up to **county**
  (reusing the A4 county-FIPS join, household-weighted by ``n_locations``) and to **state**,
  ranked by at-risk households — the prioritised list a state broadband officer acts on.
- **Interactive map** — ``build_map`` writes a per-location GeoJSON point layer coloured by
  risk tier, tiles it to **PMTiles** with ``tippecanoe`` (scales to ~1M points, no server),
  and emits a self-contained **MapLibre** HTML viewer (the +15% bonus).
- **Decision log** — ``decision_log_markdown`` renders a plain-English report (headline
  numbers, the priority-county table, caveats, the A4 anomaly / H2 status). The agentic A5
  path lets the LLM enrich the narrative; this template is the no-API baseline ``--compute``
  writes.

The county/lon-lat joins + the locations read are assembled by the tool/CLI layer and passed
in, exactly as A3 keeps geometry in the engine and A4 keeps the DuckDB read in the tool.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from leo_pipeline.config import REPORTING, Reporting
from leo_pipeline.qa import AT_RISK_TIERS, SCORED_TIERS, summarize_findings
from leo_pipeline.state import ReportArtifact

# FIPS state code -> name, so the state rollup reads "North Carolina", not "37". Static and
# tiny; counties are left as FIPS (a TIGER/Line name join is noted as a caveat instead).
STATE_FIPS_NAMES: dict[str, str] = {
    "01": "Alabama", "02": "Alaska", "04": "Arizona", "05": "Arkansas", "06": "California",
    "08": "Colorado", "09": "Connecticut", "10": "Delaware", "11": "District of Columbia",
    "12": "Florida", "13": "Georgia", "15": "Hawaii", "16": "Idaho", "17": "Illinois",
    "18": "Indiana", "19": "Iowa", "20": "Kansas", "21": "Kentucky", "22": "Louisiana",
    "23": "Maine", "24": "Maryland", "25": "Massachusetts", "26": "Michigan", "27": "Minnesota",
    "28": "Mississippi", "29": "Missouri", "30": "Montana", "31": "Nebraska", "32": "Nevada",
    "33": "New Hampshire", "34": "New Jersey", "35": "New Mexico", "36": "New York",
    "37": "North Carolina", "38": "North Dakota", "39": "Ohio", "40": "Oklahoma", "41": "Oregon",
    "42": "Pennsylvania", "44": "Rhode Island", "45": "South Carolina", "46": "South Dakota",
    "47": "Tennessee", "48": "Texas", "49": "Utah", "50": "Vermont", "51": "Virginia",
    "53": "Washington", "54": "West Virginia", "55": "Wisconsin", "56": "Wyoming",
}

# 5-digit county FIPS -> name, for the map's county filter dropdown labels. The AOI is North
# Carolina (state 37), so only NC's 100 counties are enumerated; any FIPS not listed falls back
# to its raw code (the decision-log caveat "county labels are FIPS, join TIGER for names" still
# holds for the tables). Codes are the standard NC county FIPS (37001..37199, odd).
COUNTY_FIPS_NAMES: dict[str, str] = {
    "37001": "Alamance", "37003": "Alexander", "37005": "Alleghany", "37007": "Anson",
    "37009": "Ashe", "37011": "Avery", "37013": "Beaufort", "37015": "Bertie",
    "37017": "Bladen", "37019": "Brunswick", "37021": "Buncombe", "37023": "Burke",
    "37025": "Cabarrus", "37027": "Caldwell", "37029": "Camden", "37031": "Carteret",
    "37033": "Caswell", "37035": "Catawba", "37037": "Chatham", "37039": "Cherokee",
    "37041": "Chowan", "37043": "Clay", "37045": "Cleveland", "37047": "Columbus",
    "37049": "Craven", "37051": "Cumberland", "37053": "Currituck", "37055": "Dare",
    "37057": "Davidson", "37059": "Davie", "37061": "Duplin", "37063": "Durham",
    "37065": "Edgecombe", "37067": "Forsyth", "37069": "Franklin", "37071": "Gaston",
    "37073": "Gates", "37075": "Graham", "37077": "Granville", "37079": "Greene",
    "37081": "Guilford", "37083": "Halifax", "37085": "Harnett", "37087": "Haywood",
    "37089": "Henderson", "37091": "Hertford", "37093": "Hoke", "37095": "Hyde",
    "37097": "Iredell", "37099": "Jackson", "37101": "Johnston", "37103": "Jones",
    "37105": "Lee", "37107": "Lenoir", "37109": "Lincoln", "37111": "McDowell",
    "37113": "Macon", "37115": "Madison", "37117": "Martin", "37119": "Mecklenburg",
    "37121": "Mitchell", "37123": "Montgomery", "37125": "Moore", "37127": "Nash",
    "37129": "New Hanover", "37131": "Northampton", "37133": "Onslow", "37135": "Orange",
    "37137": "Pamlico", "37139": "Pasquotank", "37141": "Pender", "37143": "Perquimans",
    "37145": "Person", "37147": "Pitt", "37149": "Polk", "37151": "Randolph",
    "37153": "Richmond", "37155": "Robeson", "37157": "Rockingham", "37159": "Rowan",
    "37161": "Rutherford", "37163": "Sampson", "37165": "Scotland", "37167": "Stanly",
    "37169": "Stokes", "37171": "Surry", "37173": "Swain", "37175": "Transylvania",
    "37177": "Tyrrell", "37179": "Union", "37181": "Vance", "37183": "Wake",
    "37185": "Warren", "37187": "Washington", "37189": "Watauga", "37191": "Wayne",
    "37193": "Wilkes", "37195": "Wilson", "37197": "Yadkin", "37199": "Yancey",
}


def _weight(lid: str, weight_of: Mapping[str, float] | None) -> float:
    """Household weight for a finding — ``n_locations`` collapsed onto its unique coordinate,
    so aggregates count households rather than unique-coordinate work-items. Defaults to 1."""
    if not weight_of:
        return 1.0
    return float(weight_of.get(lid, 1.0))


def _region_row(region: str, items: list[Mapping[str, Any]], weight_of) -> dict[str, Any]:
    """Household-weighted tier breakdown + at-risk rate for one region (county or state)."""
    tier_w: dict[str, float] = {}
    total_w = 0.0
    for f in items:
        w = _weight(str(f.get("location_id")), weight_of)
        total_w += w
        tier_w[f.get("risk_tier") or "undetermined"] = (
            tier_w.get(f.get("risk_tier") or "undetermined", 0.0) + w
        )
    scored_w = sum(tier_w.get(t, 0.0) for t in SCORED_TIERS)
    at_risk_w = sum(tier_w.get(t, 0.0) for t in AT_RISK_TIERS)
    return {
        "region": region,
        "households": int(round(total_w)),
        "tier_households": {t: int(round(v)) for t, v in sorted(tier_w.items())},
        "at_risk_households": int(round(at_risk_w)),
        "at_risk_rate": round(at_risk_w / scored_w, 4) if scored_w else 0.0,
        "undetermined_households": int(round(tier_w.get("undetermined", 0.0))),
    }


def aggregate_findings(
    findings: list[Mapping[str, Any]],
    *,
    county_of: Mapping[str, str] | None = None,
    weight_of: Mapping[str, float] | None = None,
    report: Reporting = REPORTING,
) -> dict[str, Any]:
    """Roll findings up to county + state, household-weighted, ranked by at-risk households.

    ``county_of[location_id] -> county FIPS`` (from the dedup-map join) drives the grouping;
    without it only run totals are produced. Counties are ranked most-at-risk first; the full
    ranking is returned (the decision log spotlights the top ``top_n_counties``)."""
    summary = summarize_findings(findings)
    out: dict[str, Any] = {
        "report_version": report.report_version,
        "summary": summary,
        "grouped_by": [],
        "counties": [],
        "states": [],
    }
    if not county_of:
        return out

    by_county: dict[str, list[Mapping[str, Any]]] = {}
    by_state: dict[str, list[Mapping[str, Any]]] = {}
    for f in findings:
        fips = county_of.get(str(f.get("location_id")))
        if not fips:
            continue
        by_county.setdefault(fips, []).append(f)
        by_state.setdefault(fips[:2], []).append(f)

    counties = [_region_row(fips, items, weight_of) for fips, items in by_county.items()]
    counties.sort(key=lambda r: (-r["at_risk_households"], r["region"]))
    states = [_region_row(fips, items, weight_of) for fips, items in by_state.items()]
    for s in states:
        s["state_name"] = STATE_FIPS_NAMES.get(s["region"], s["region"])
    states.sort(key=lambda r: (-r["at_risk_households"], r["region"]))

    out["grouped_by"] = ["county", "state"]
    out["counties"] = counties
    out["states"] = states
    return out


# --- interactive map: GeoJSON -> PMTiles (tippecanoe) -> MapLibre HTML ----------------


def findings_to_geojson(
    findings: list[Mapping[str, Any]],
    lonlat_of: Mapping[str, tuple[float, float]] | None = None,
    county_of: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """A point ``FeatureCollection`` of the findings coloured downstream by ``risk_tier``.

    Coordinates come from the finding itself (``lon``/``lat``) when present, else from
    ``lonlat_of[location_id]`` (built from the unique-coordinate work list). Findings with no
    resolvable coordinate are skipped (they cannot be placed).

    ``county_of[location_id] -> 5-digit county FIPS`` (the same dedup-map join the aggregates
    use) is baked onto each feature as ``county`` + ``state`` (FIPS[:2]) so the MapLibre viewer
    can filter by county/state tile-side. Points with no county join carry neither (they show
    only under the "All" selections)."""
    features = []
    for f in findings:
        lid = str(f.get("location_id"))
        lon, lat = f.get("lon"), f.get("lat")
        if (lon is None or lat is None) and lonlat_of and lid in lonlat_of:
            lon, lat = lonlat_of[lid]
        if lon is None or lat is None:
            continue
        props: dict[str, Any] = {
            "location_id": lid,
            "risk_tier": f.get("risk_tier") or "undetermined",
            "obstruction_pct": f.get("obstruction_pct"),
            "confidence": f.get("confidence"),
        }
        fips = county_of.get(lid) if county_of else None
        if fips:
            props["county"] = str(fips)
            props["state"] = str(fips)[:2]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _bbox_center(geojson: Mapping[str, Any]) -> tuple[list[float], float]:
    """(center [lon,lat], zoom) covering the features — a sane initial map view."""
    feats = geojson.get("features") or []
    if not feats:
        return [-79.9, 35.2], 6.0  # AOI-ish fallback (central Carolinas)
    lons = [g["geometry"]["coordinates"][0] for g in feats]
    lats = [g["geometry"]["coordinates"][1] for g in feats]
    cx, cy = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2
    span = max(max(lons) - min(lons), max(lats) - min(lats), 0.01)
    # Rough span->zoom: ~ log2(360/span), clamped to a usable range.
    import math

    zoom = max(3.0, min(11.0, math.log2(360.0 / span)))
    return [round(cx, 5), round(cy, 5)], round(zoom, 2)


def tippecanoe_available() -> bool:
    return shutil.which("tippecanoe") is not None


def run_tippecanoe(
    geojson_path: Path, pmtiles_path: Path, report: Reporting = REPORTING
) -> tuple[bool, str]:
    """Tile a GeoJSON point layer to PMTiles. Returns (ok, message); never raises on a missing
    binary or a non-zero exit — A5 degrades to the GeoJSON layer and says so."""
    if not tippecanoe_available():
        return False, "tippecanoe not on PATH — skipped PMTiles (GeoJSON layer still written)"
    cmd = [
        "tippecanoe", "-o", str(pmtiles_path), "--force",
        "-l", report.map_layer_name,
        "-Z", str(report.map_min_zoom), "-z", str(report.map_max_zoom),
        "-r1", "--cluster-distance=8",  # drop-densest-as-needed alternative: keep all but cluster
        str(geojson_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as exc:  # subprocess failure, timeout, ...
        return False, f"tippecanoe failed to run: {exc}"
    if proc.returncode != 0:
        return False, f"tippecanoe exited {proc.returncode}: {proc.stderr.strip()[:300]}"
    return True, f"wrote {pmtiles_path.name}"


def maplibre_html(
    pmtiles_filename: str,
    *,
    center: list[float],
    zoom: float,
    title: str = "LEO coverage-risk map",
    report: Reporting = REPORTING,
    aggregates: Mapping[str, Any] | None = None,
) -> str:
    """A self-contained MapLibre + PMTiles viewer: satellite basemap + DEM terrain/hillshade/sky,
    a circle layer coloured by ``risk_tier``, click popups, a toggleable legend, and **state +
    county filter dropdowns** that combine with the tier toggles. Pure string — no I/O — so it
    stays unit testable and the PMTiles file is referenced by a relative path next to the HTML.

    ``aggregates`` (the county/state rollup) supplies the dropdown options: ``states`` ->
    ``state`` choices, ``counties`` -> ``county`` choices (labelled via ``COUNTY_FIPS_NAMES``,
    falling back to the raw FIPS). With no ``aggregates`` the panel shows only the "All" rows."""
    colors = report.tier_colors
    match_expr = ["match", ["get", "risk_tier"]]
    for tier, color in colors.items():
        match_expr += [tier, color]
    match_expr += ["#888888"]  # fallback colour
    legend_rows = "".join(
        f'<div class="row" data-tier="{t}"><span style="background:{c}"></span>{t}</div>'
        for t, c in colors.items()
    )
    tiers_json = json.dumps(list(colors.keys()))

    states = (aggregates or {}).get("states") or []
    counties = (aggregates or {}).get("counties") or []
    state_opts = "".join(
        f'<option value="{s["region"]}">{s.get("state_name", s["region"])}</option>'
        for s in states
    )
    # Counties alphabetised by display name for a usable dropdown (aggregates are at-risk-ranked).
    county_labelled = sorted(
        (
            (str(c["region"]), COUNTY_FIPS_NAMES.get(str(c["region"]), str(c["region"])))
            for c in counties
        ),
        key=lambda fc: fc[1],
    )
    county_opts = "".join(
        f'<option value="{fips}" data-state="{fips[:2]}">{name} ({fips})</option>'
        for fips, name in county_labelled
    )

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://unpkg.com/pmtiles@3.2.1/dist/pmtiles.js"></script>
<style>
  html,body,#map{{margin:0;height:100%;width:100%}}
  .panel{{position:absolute;top:10px;left:10px;background:#fff;padding:8px 10px;
    font:13px/1.4 sans-serif;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.3);min-width:170px}}
  .panel b{{display:block;margin-bottom:6px}}
  .panel label{{display:block;color:#555;font-size:11px;margin:6px 0 2px}}
  .panel select{{width:100%;font:12px sans-serif;padding:2px}}
  .legend{{position:absolute;bottom:18px;left:10px;background:#fff;padding:8px 10px;
    font:13px/1.4 sans-serif;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.3)}}
  .legend b{{display:block;margin-bottom:4px}}
  .legend .row{{cursor:pointer;user-select:none;display:flex;align-items:center;padding:1px 0}}
  .legend .row span{{display:inline-block;width:12px;height:12px;margin-right:6px;border-radius:50%;vertical-align:middle}}
  .legend .row.off{{opacity:.4}}
  .legend .row.off span{{box-shadow:inset 0 0 0 2px #fff}}
  .legend .hint{{margin-top:5px;color:#666;font-size:11px}}
</style></head><body>
<div id="map"></div>
<div class="panel"><b>Filters</b>
  <label for="stateSel">State</label>
  <select id="stateSel"><option value="">All states</option>{state_opts}</select>
  <label for="countySel">County</label>
  <select id="countySel"><option value="">All counties</option>{county_opts}</select>
</div>
<div class="legend"><b>{title}</b>{legend_rows}
  <div class="hint">click a tier to toggle</div>
</div>
<script>
  let protocol = new pmtiles.Protocol();
  maplibregl.addProtocol("pmtiles", protocol.tile);
  const map = new maplibregl.Map({{
    container: "map",
    style: {{ version: 8,
      sources: {{
        sat: {{ type: "raster",
          tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}"],
          tileSize: 256, maxzoom: 19,
          attribution: "Imagery © Esri, Maxar, Earthstar Geographics" }},
        dem: {{ type: "raster-dem",
          tiles: ["https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{{z}}/{{x}}/{{y}}.png"],
          tileSize: 256, maxzoom: 14, encoding: "terrarium",
          attribution: "Elevation: Mapzen / AWS Terrain Tiles" }}
      }},
      layers: [
        {{ id: "sat", type: "raster", source: "sat" }},
        {{ id: "hillshade", type: "hillshade", source: "dem",
          paint: {{ "hillshade-exaggeration": 0.4 }} }}
      ] }},
    center: {center}, zoom: {zoom}, pitch: 55, maxPitch: 80
  }});
  map.addControl(new maplibregl.NavigationControl({{ visualizePitch: true }}), "top-right");

  map.on("load", () => {{
    map.setTerrain({{ source: "dem", exaggeration: 1.4 }});
    map.setSky({{
      "sky-color": "#88b8e8", "sky-horizon-blend": 0.5,
      "horizon-color": "#dfeefc", "horizon-fog-blend": 0.6,
      "fog-color": "#e8eef4", "fog-ground-blend": 0.4
    }});

    map.addSource("loc", {{ type: "vector", url: "pmtiles://./{pmtiles_filename}" }});
    map.addLayer({{ id: "loc", type: "circle", source: "loc",
      "source-layer": "{report.map_layer_name}",
      paint: {{
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 1.5, 12, 5],
        "circle-color": {json.dumps(match_expr)},
        "circle-opacity": 0.85, "circle-stroke-width": 0.3, "circle-stroke-color": "#333"
      }} }});

    // Combined filter: active risk tiers (legend toggles) AND the state/county dropdowns.
    const active = new Set({tiers_json});
    const stateSel = document.getElementById("stateSel");
    const countySel = document.getElementById("countySel");
    const allCountyOptions = Array.from(countySel.options).map((o) => o.cloneNode(true));
    const applyFilter = () => {{
      const preds = [["in", ["get", "risk_tier"], ["literal", [...active]]]];
      if (stateSel.value) preds.push(["==", ["get", "state"], stateSel.value]);
      if (countySel.value) preds.push(["==", ["get", "county"], countySel.value]);
      map.setFilter("loc", ["all", ...preds]);
    }};

    document.querySelectorAll(".legend .row").forEach((row) => {{
      row.addEventListener("click", () => {{
        const t = row.dataset.tier;
        if (active.has(t)) {{ active.delete(t); row.classList.add("off"); }}
        else {{ active.add(t); row.classList.remove("off"); }}
        applyFilter();
      }});
    }});

    // Cascade: narrow the county list to the chosen state (reset to All on change).
    const repopulateCounties = () => {{
      const st = stateSel.value;
      countySel.innerHTML = "";
      allCountyOptions.forEach((o) => {{
        if (!o.value || !st || o.dataset.state === st) countySel.appendChild(o.cloneNode(true));
      }});
      countySel.value = "";
    }};
    stateSel.addEventListener("change", () => {{ repopulateCounties(); applyFilter(); }});
    countySel.addEventListener("change", applyFilter);

    map.on("click", "loc", (e) => {{
      const p = e.features[0].properties;
      new maplibregl.Popup().setLngLat(e.lngLat)
        .setHTML("<b>" + p.location_id + "</b><br>tier: " + p.risk_tier +
                 "<br>obstruction: " + p.obstruction_pct + "%<br>confidence: " + p.confidence)
        .addTo(map);
    }});
    map.on("mouseenter", "loc", () => map.getCanvas().style.cursor = "pointer");
    map.on("mouseleave", "loc", () => map.getCanvas().style.cursor = "");
  }});
</script></body></html>
"""


def build_map(
    findings: list[Mapping[str, Any]],
    out_dir: Path,
    *,
    lonlat_of: Mapping[str, tuple[float, float]] | None = None,
    county_of: Mapping[str, str] | None = None,
    aggregates: Mapping[str, Any] | None = None,
    run_tiles: bool = True,
    report: Reporting = REPORTING,
) -> dict[str, Any]:
    """Write the GeoJSON layer, tile it to PMTiles (best-effort), and emit the MapLibre HTML.
    Returns artifact paths + a note; degrades gracefully if tippecanoe is unavailable.

    ``county_of`` bakes county/state onto each point (so the viewer can filter by them) and
    ``aggregates`` (the county/state rollup) populates the filter dropdowns."""
    out_dir.mkdir(parents=True, exist_ok=True)
    geojson = findings_to_geojson(findings, lonlat_of, county_of)
    geojson_path = out_dir / "locations.geojson"
    geojson_path.write_text(json.dumps(geojson))

    center, zoom = _bbox_center(geojson)
    pmtiles_path = out_dir / "locations.pmtiles"
    note = "PMTiles tiling skipped (run_tiles=False)"
    pmtiles_written = False
    if run_tiles:
        pmtiles_written, note = run_tippecanoe(geojson_path, pmtiles_path, report)

    html = maplibre_html(
        pmtiles_path.name, center=center, zoom=zoom, report=report, aggregates=aggregates
    )
    html_path = out_dir / "coverage_map.html"
    html_path.write_text(html)

    result = {
        "n_features": len(geojson["features"]),
        "geojson": str(geojson_path),
        "html": str(html_path),
        "center": center,
        "zoom": zoom,
        "note": note,
    }
    if pmtiles_written:
        result["pmtiles"] = str(pmtiles_path)
    return result


# --- officer-facing decision log -----------------------------------------------------


def _pct(part: float, whole: float) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "—"


def decision_log_markdown(
    aggregates: Mapping[str, Any],
    *,
    anomalies: list[Mapping[str, Any]] | None = None,
    map_rel: str | None = None,
    generated_at: str | None = None,
    report: Reporting = REPORTING,
) -> str:
    """Render the plain-English decision log a broadband officer reads. Deterministic baseline;
    the agentic A5 path may replace the prose, but the numbers/tables come from here."""
    summary = aggregates["summary"]
    tiers = summary["tier_counts"]
    n = summary["n_findings"]
    specs = summary.get("spec_versions") or ["(unstamped)"]
    when = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines = [
        "# LEO coverage-risk — assessment summary",
        "",
        f"*Generated {when} · report `{report.report_version}` · "
        f"obstruction spec `{', '.join(specs)}`*",
        "",
        "## What this is",
        "",
        "For every funded location we checked whether trees, hills, or nearby buildings would "
        "block the slice of sky a Starlink dish needs to reach its satellites — the same check "
        "the Starlink app runs at install, but from public maps instead of a rooftop scan. "
        "Treat this as a **prioritised list of places to verify on site**, not a guarantee of "
        "service: we cannot see the exact roof or how high the installer will mount the dish.",
        "",
        "## Headline",
        "",
        f"- **{n:,}** locations assessed.",
        f"- 🟢 clear: **{tiers.get('clear', 0):,}** ({_pct(tiers.get('clear', 0), n)})",
        f"- 🟡 at-risk (marginal): **{tiers.get('at_risk', 0):,}** ({_pct(tiers.get('at_risk', 0), n)})",
        f"- 🔴 severe: **{tiers.get('severe', 0):,}** ({_pct(tiers.get('severe', 0), n)})",
        f"- ⚪ undetermined (data too thin to judge): "
        f"**{tiers.get('undetermined', 0):,}** ({_pct(tiers.get('undetermined', 0), n)})",
    ]

    states = aggregates.get("states") or []
    if states:
        lines += ["", "## By state", "", "| State | Households | At-risk | At-risk rate | Undetermined |",
                  "|---|--:|--:|--:|--:|"]
        for s in states:
            lines.append(
                f"| {s.get('state_name', s['region'])} | {s['households']:,} | "
                f"{s['at_risk_households']:,} | {s['at_risk_rate']:.1%} | "
                f"{s['undetermined_households']:,} |"
            )

    counties = aggregates.get("counties") or []
    if counties:
        top = counties[: report.top_n_counties]
        lines += ["", f"## Priority counties (top {len(top)} by at-risk households)", "",
                  "| County FIPS | State | Households | At-risk | At-risk rate |",
                  "|---|---|--:|--:|--:|"]
        for c in top:
            lines.append(
                f"| {c['region']} | {STATE_FIPS_NAMES.get(c['region'][:2], c['region'][:2])} | "
                f"{c['households']:,} | {c['at_risk_households']:,} | {c['at_risk_rate']:.1%} |"
            )
        if len(counties) > len(top):
            lines.append(f"\n*…and {len(counties) - len(top):,} more counties — see the JSON sidecar.*")

    lines += ["", "## Caveats & confidence", ""]
    u_rate = summary.get("undetermined_rate", 0.0)
    l_rate = summary.get("low_confidence_rate", 0.0)
    lines += [
        f"- **{u_rate:.1%}** of locations are *undetermined* and **{l_rate:.1%}** are "
        "low-confidence — these need on-site verification before any funding decision.",
        "- County labels are FIPS codes; join TIGER/Line for plain names.",
        "- The 🟡/🔴 cut-points are calibration defaults (see `docs/rationale.md`) and must be "
        "tuned against the Starlink app on a labelled sample before being treated as ground truth.",
    ]
    if anomalies:
        crit = [a for a in anomalies if a.get("severity") == "critical"]
        lines += [
            "",
            "## QA status (H2 gate)",
            "",
            f"- A4 flagged **{len(anomalies)}** anomaly(ies)"
            + (f", **{len(crit)} critical** — do NOT publish before review." if crit
               else " for human review before sign-off."),
        ]
        for a in anomalies[:10]:
            lines.append(f"  - `[{a.get('severity')}]` {a.get('rule')} ({a.get('scope')}): {a.get('detail')}")

    if map_rel:
        lines += ["", "## Map", "", f"Interactive risk map: [`{map_rel}`]({map_rel}) "
                  "(open in a browser; points coloured by risk tier)."]
    lines.append("")
    return "\n".join(lines)


# --- top-level build (the --compute path / agentic baseline) -------------------------


def build_report(
    findings: list[Mapping[str, Any]],
    out_dir: Path,
    *,
    county_of: Mapping[str, str] | None = None,
    weight_of: Mapping[str, float] | None = None,
    lonlat_of: Mapping[str, tuple[float, float]] | None = None,
    anomalies: list[Mapping[str, Any]] | None = None,
    run_tiles: bool = True,
    report: Reporting = REPORTING,
) -> dict[str, Any]:
    """Build all three A5 deliverables and write them to ``out_dir``: the aggregates JSON, the
    interactive map (GeoJSON + PMTiles + HTML), and the decision-log markdown. Returns the
    aggregates + a list of :class:`ReportArtifact` (as dicts) referencing each file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    aggregates = aggregate_findings(
        findings, county_of=county_of, weight_of=weight_of, report=report
    )
    agg_path = out_dir / "aggregates.json"
    agg_path.write_text(json.dumps(aggregates, indent=2, default=str))

    map_info = build_map(
        findings, out_dir, lonlat_of=lonlat_of, county_of=county_of,
        aggregates=aggregates, run_tiles=run_tiles, report=report,
    )
    log_md = decision_log_markdown(
        aggregates, anomalies=anomalies, map_rel=Path(map_info["html"]).name, report=report
    )
    log_path = out_dir / "decision_log.md"
    log_path.write_text(log_md)

    artifacts = [
        ReportArtifact("aggregates", str(agg_path), "county + state household-weighted rollup"),
        ReportArtifact("geojson", map_info["geojson"], "per-location point layer (risk tier)"),
        ReportArtifact("map_html", map_info["html"], "self-contained MapLibre risk map"),
        ReportArtifact("decision_log", str(log_path), "officer-facing plain-English summary"),
    ]
    if "pmtiles" in map_info:
        artifacts.insert(2, ReportArtifact("pmtiles", map_info["pmtiles"], "vector tiles for the map"))

    return {
        "report_version": report.report_version,
        "out_dir": str(out_dir),
        "aggregates": aggregates,
        "map": map_info,
        "artifacts": [vars(a) for a in artifacts],
    }
