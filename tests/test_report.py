"""Deterministic unit tests for the A5 reporting engine (leo_pipeline.report).

No network and no API key: aggregation + GeoJSON + the MapLibre HTML builder + the decision
log run over small synthetic finding dicts, and the PMTiles step is tested only when
``tippecanoe`` is on PATH (skipped otherwise). The engine backs the aggregate_findings /
render_map tools.
"""

from __future__ import annotations

import json

import pytest

from leo_pipeline import report
from leo_pipeline.config import REPORTING


def _f(i, tier, pct, *, lon=None, lat=None):
    return {
        "location_id": f"coord_{i}", "tile_id": "T", "obstruction_pct": pct,
        "risk_tier": tier, "confidence": "high", "spec_version": "spec-2026.06-theta25",
        **({"lon": lon} if lon is not None else {}),
        **({"lat": lat} if lat is not None else {}),
    }


def _mixed():
    # 5 clear, 3 severe, 2 at_risk
    return (
        [_f(i, "clear", 0.0) for i in range(5)]
        + [_f(5 + i, "severe", 40.0) for i in range(3)]
        + [_f(8 + i, "at_risk", 5.0) for i in range(2)]
    )


# --- aggregation ---------------------------------------------------------------------


def test_aggregate_county_state_household_weighted_and_ranked():
    findings = _mixed()
    # first 6 in NC county 37119, rest in SC county 45019; 10 households per coord
    county_of = {f["location_id"]: ("37119" if i < 6 else "45019") for i, f in enumerate(findings)}
    weight_of = {f["location_id"]: 10.0 for f in findings}
    agg = report.aggregate_findings(findings, county_of=county_of, weight_of=weight_of)

    assert agg["grouped_by"] == ["county", "state"]
    # SC: findings 6-9 = 2 severe + 2 at_risk -> 40 hh, all at-risk; ranked first
    assert agg["counties"][0]["region"] == "45019"
    assert agg["counties"][0]["at_risk_households"] == 40
    assert agg["counties"][0]["at_risk_rate"] == pytest.approx(1.0)
    states = {s["region"]: s for s in agg["states"]}
    assert states["45"]["state_name"] == "South Carolina"
    assert states["37"]["households"] == 60  # 6 coords * 10
    assert states["37"]["at_risk_households"] == 10  # one severe in NC


def test_aggregate_without_county_map_returns_totals_only():
    agg = report.aggregate_findings(_mixed())
    assert agg["grouped_by"] == []
    assert agg["counties"] == []
    assert agg["summary"]["n_findings"] == 10


# --- geojson -------------------------------------------------------------------------


def test_findings_to_geojson_uses_inline_and_lookup_coords():
    findings = [_f(0, "severe", 40.0, lon=-80.0, lat=35.0), _f(1, "clear", 0.0)]
    lonlat_of = {"coord_1": (-79.0, 34.0)}
    gj = report.findings_to_geojson(findings, lonlat_of)
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) == 2
    f0 = gj["features"][0]
    assert f0["geometry"]["coordinates"] == [-80.0, 35.0]
    assert f0["properties"]["risk_tier"] == "severe"


def test_findings_to_geojson_skips_unplaceable():
    gj = report.findings_to_geojson([_f(0, "clear", 0.0)])  # no coords, no lookup
    assert gj["features"] == []


def test_findings_to_geojson_includes_county_and_state():
    findings = [_f(0, "severe", 40.0, lon=-80.0, lat=35.0)]
    gj = report.findings_to_geojson(findings, county_of={"coord_0": "37119"})
    props = gj["features"][0]["properties"]
    assert props["county"] == "37119"
    assert props["state"] == "37"  # state = county FIPS[:2]


# --- MapLibre HTML (pure string) -----------------------------------------------------


def test_maplibre_html_references_pmtiles_layer_and_colors():
    html = report.maplibre_html("locations.pmtiles", center=[-80.0, 35.0], zoom=8.0)
    assert "pmtiles://./locations.pmtiles" in html
    assert f'"source-layer": "{REPORTING.map_layer_name}"' in html
    assert REPORTING.tier_colors["severe"] in html  # the risk-tier match expression
    assert "World_Imagery" in html  # satellite basemap
    assert "raster-dem" in html  # DEM terrain / hillshade overlay
    assert "[-80.0, 35.0]" in html  # initial center


def test_maplibre_html_renders_state_and_county_filters():
    aggregates = {
        "states": [{"region": "37", "state_name": "North Carolina"}],
        "counties": [{"region": "37119"}],
    }
    html = report.maplibre_html(
        "locations.pmtiles", center=[-80.0, 35.0], zoom=8.0, aggregates=aggregates
    )
    assert 'id="stateSel"' in html and 'id="countySel"' in html
    assert "North Carolina" in html  # state option
    assert 'data-state="37"' in html  # county option carries its state for cascading
    assert "Mecklenburg (37119)" in html  # FIPS-labelled county option
    assert '["all", ...preds]' in html  # combined tier + state + county filter


# --- decision log --------------------------------------------------------------------


def test_decision_log_has_headline_tables_and_caveats():
    findings = _mixed()
    county_of = {f["location_id"]: "37119" for f in findings}
    weight_of = {f["location_id"]: 1.0 for f in findings}
    agg = report.aggregate_findings(findings, county_of=county_of, weight_of=weight_of)
    md = report.decision_log_markdown(agg, map_rel="coverage_map.html")
    assert "# LEO coverage-risk" in md
    assert "## Headline" in md
    assert "Priority counties" in md
    assert "North Carolina" in md
    assert "verify on site" in md.lower()
    assert "spec-2026.06-theta25" in md
    assert "coverage_map.html" in md


def test_decision_log_flags_critical_anomalies():
    agg = report.aggregate_findings(_mixed())
    anomalies = [{"severity": "critical", "rule": "spec_version_drift", "scope": "run",
                  "detail": "two specs"}]
    md = report.decision_log_markdown(agg, anomalies=anomalies)
    assert "H2 gate" in md
    assert "critical" in md
    assert "do NOT publish" in md


# --- build_map / build_report (file I/O) ---------------------------------------------


def test_build_map_without_tiles_writes_geojson_and_html(tmp_path):
    findings = [_f(0, "severe", 40.0, lon=-80.0, lat=35.0)]
    info = report.build_map(findings, tmp_path, run_tiles=False)
    assert info["n_features"] == 1
    assert (tmp_path / "locations.geojson").exists()
    assert (tmp_path / "coverage_map.html").exists()
    assert "pmtiles" not in info  # tiling skipped
    assert "skipped" in info["note"]


def test_build_report_writes_all_artifacts(tmp_path):
    findings = _mixed()
    for i, f in enumerate(findings):  # give them coords so the map has features
        f["lon"], f["lat"] = -80.0 + 0.001 * i, 35.0 + 0.001 * i
    county_of = {f["location_id"]: "37119" for f in findings}
    weight_of = {f["location_id"]: 1.0 for f in findings}
    result = report.build_report(
        findings, tmp_path, county_of=county_of, weight_of=weight_of, run_tiles=False
    )
    kinds = {a["kind"] for a in result["artifacts"]}
    assert {"aggregates", "geojson", "map_html", "decision_log"} <= kinds
    assert (tmp_path / "aggregates.json").exists()
    assert (tmp_path / "decision_log.md").exists()
    # aggregates JSON is valid and carries the county rollup
    agg = json.loads((tmp_path / "aggregates.json").read_text())
    assert agg["counties"][0]["region"] == "37119"


@pytest.mark.skipif(not report.tippecanoe_available(), reason="tippecanoe not installed")
def test_build_map_with_tippecanoe_writes_pmtiles(tmp_path):
    findings = [_f(i, "clear", 0.0, lon=-80.0 + 0.001 * i, lat=35.0 + 0.001 * i) for i in range(20)]
    info = report.build_map(findings, tmp_path, run_tiles=True)
    assert "pmtiles" in info
    assert (tmp_path / "locations.pmtiles").exists()
    assert (tmp_path / "locations.pmtiles").stat().st_size > 0
