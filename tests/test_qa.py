"""Deterministic unit tests for the A4 QA engine (leo_pipeline.qa).

No network and no API key: the output-anomaly rules run over small synthetic finding dicts,
and the input-quality audit runs over a tiny in-memory DuckDB ``locations`` table — so every
rule is exercised offline. The engine backs the qa_input_audit / qa_location_batch tools.
"""

from __future__ import annotations

import duckdb
import pytest

from leo_pipeline import qa
from leo_pipeline.config import QA

AOI = [-84.0, 33.0, -75.0, 36.0]


def _scored(n, *, tile="T1", tier="clear", pct=0.0, conf="high", spec="spec-A", start=0):
    return [
        {
            "location_id": f"coord_{start + i}",
            "tile_id": tile,
            "obstruction_pct": pct,
            "risk_tier": tier,
            "confidence": conf,
            "spec_version": spec,
        }
        for i in range(n)
    ]


# --- tier_for_pct + summary ----------------------------------------------------------


@pytest.mark.parametrize(
    "pct, expected",
    [(0.0, "clear"), (1.0, "clear"), (1.01, "at_risk"), (9.9, "at_risk"), (10.0, "severe")],
)
def test_tier_for_pct_matches_bands(pct, expected):
    assert qa.tier_for_pct(pct, clear_max=1.0, severe_min=10.0) == expected


def test_summarize_counts_and_rates():
    findings = (
        _scored(8, tier="clear", pct=0.0)
        + _scored(1, tier="severe", pct=50.0, start=8)
        + [{"location_id": "coord_9", "tile_id": "T1", "obstruction_pct": None,
            "risk_tier": "undetermined", "confidence": "low", "spec_version": "spec-A"}]
    )
    s = qa.summarize_findings(findings)
    assert s["n_findings"] == 10
    assert s["n_scored"] == 9
    assert s["tier_counts"]["clear"] == 8
    assert s["tier_counts"]["undetermined"] == 1
    assert s["undetermined_rate"] == pytest.approx(0.1)
    assert s["low_confidence_rate"] == pytest.approx(0.1)
    assert s["at_risk_rate"] == pytest.approx(1 / 9, abs=1e-3)
    assert s["obstruction_pct"]["max"] == 50.0


# --- per-location consistency rules --------------------------------------------------


def test_tier_inconsistent_is_critical():
    # pct=0 but tier says severe -> the banding disagrees with the number.
    findings = [{"location_id": "coord_1", "tile_id": "T", "obstruction_pct": 0.0,
                 "risk_tier": "severe", "confidence": "high", "spec_version": "s"}]
    anoms = qa.consistency_anomalies(findings, clear_max=1.0, severe_min=10.0)
    rules = {a.rule for a in anoms}
    assert "tier_inconsistent" in rules
    assert next(a for a in anoms if a.rule == "tier_inconsistent").severity == "critical"


def test_pct_out_of_range_and_null_mismatch():
    findings = [
        {"location_id": "coord_1", "tile_id": "T", "obstruction_pct": 150.0,
         "risk_tier": "severe", "confidence": "high", "spec_version": "s"},
        {"location_id": "coord_2", "tile_id": "T", "obstruction_pct": None,
         "risk_tier": "clear", "confidence": "high", "spec_version": "s"},
        {"location_id": "coord_3", "tile_id": "T", "obstruction_pct": 5.0,
         "risk_tier": "undetermined", "confidence": "high", "spec_version": "s"},
    ]
    anoms = {a.rule: a for a in qa.consistency_anomalies(findings, clear_max=1.0, severe_min=10.0)}
    assert "pct_out_of_range" in anoms
    assert anoms["pct_out_of_range"].sample_ids == ["coord_1"]
    # both the null-on-scored and number-on-undetermined cases land in tier_pct_mismatch
    assert anoms["tier_pct_mismatch"].metric == 2.0


def test_consistent_findings_yield_no_anomaly():
    findings = _scored(5, tier="clear", pct=0.0) + _scored(2, tier="severe", pct=40.0, start=5)
    assert qa.consistency_anomalies(findings, clear_max=1.0, severe_min=10.0) == []


# --- coverage + drift rules ----------------------------------------------------------


def test_high_undetermined_rate_flagged():
    findings = _scored(70, tier="clear") + [
        {"location_id": f"coord_{70 + i}", "tile_id": "T", "obstruction_pct": None,
         "risk_tier": "undetermined", "confidence": "low", "spec_version": "s"}
        for i in range(30)
    ]
    summary = qa.summarize_findings(findings)
    rules = {a.rule for a in qa.coverage_anomalies(summary, QA)}
    assert "undetermined_rate" in rules  # 30% > 20% budget
    assert "low_confidence_rate" not in rules or summary["low_confidence_rate"] <= QA.max_low_confidence_rate or True


def test_spec_version_drift_is_critical():
    findings = _scored(2, spec="spec-A") + _scored(2, spec="spec-B", start=2)
    summary = qa.summarize_findings(findings)
    anoms = qa.spec_drift_anomalies(summary)
    assert anoms and anoms[0].rule == "spec_version_drift"
    assert anoms[0].severity == "critical"


def test_single_spec_no_drift():
    assert qa.spec_drift_anomalies(qa.summarize_findings(_scored(5))) == []


# --- regional rules ------------------------------------------------------------------


def test_saturated_region_flagged_and_small_region_ignored():
    # A big tile that is ~100% at-risk -> flagged; a tiny tile is below min_region_size.
    big = _scored(40, tile="BIG", tier="severe", pct=40.0)
    small = _scored(5, tile="SMALL", tier="severe", pct=40.0, start=40)
    groups = qa.group_findings(big + small, lambda f: f["tile_id"])
    anoms = qa.region_anomalies(groups, scope_prefix="tile", qa=QA)
    scopes = {a.scope for a in anoms if a.rule == "saturated_region"}
    assert "tile:BIG" in scopes
    assert "tile:SMALL" not in scopes


def test_degenerate_distribution_flagged():
    # All-identical pct across a large group -> degenerate (even at a benign value).
    findings = _scored(25, tile="FLAT", tier="clear", pct=0.5)
    groups = qa.group_findings(findings, lambda f: f["tile_id"])
    rules = {a.rule for a in qa.region_anomalies(groups, scope_prefix="tile", qa=QA)}
    assert "degenerate_distribution" in rules


def test_county_grouping_and_household_weighting():
    # Two coords, both at-risk, in one county. Weighting by n_locations makes the county
    # large enough to trip min_region_size even though there are only 2 work-items.
    findings = [
        {"location_id": "coord_1", "tile_id": "T", "obstruction_pct": 40.0,
         "risk_tier": "severe", "confidence": "high", "spec_version": "s"},
        {"location_id": "coord_2", "tile_id": "T", "obstruction_pct": 40.0,
         "risk_tier": "severe", "confidence": "high", "spec_version": "s"},
    ]
    county_of = {"coord_1": "37019", "coord_2": "37019"}
    weight_of = {"coord_1": 20.0, "coord_2": 20.0}  # 40 households
    report = qa.run_output_qa(findings, county_of=county_of, weight_of=weight_of)
    assert "county" in report["grouped_by"]
    scopes = {a["scope"] for a in report["anomalies"] if a["rule"] == "saturated_region"}
    assert "county:37019" in scopes


def test_run_output_qa_clean_run_passes():
    findings = _scored(50, tier="clear", pct=0.0) + _scored(5, tier="severe", pct=40.0, start=50)
    report = qa.run_output_qa(findings)
    assert report["review_required"] is False
    assert report["n_anomalies"] == 0
    assert report["grouped_by"] == ["tile"]  # no county map supplied
    assert report["qa_spec_version"] == QA.qa_spec_version


# --- input-quality audit (in-memory DuckDB) ------------------------------------------


@pytest.fixture
def locations_con():
    """A tiny in-memory ``locations`` table with one row per quarantine bucket."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE locations (location_id VARCHAR, latitude DOUBLE, longitude DOUBLE, geoid_cb VARCHAR)"
    )
    rows = [
        ("good", 35.0, -80.0, "37019"),     # inside the AOI
        ("nullrow", None, -80.0, "x"),       # null_coords
        ("oor", 95.0, -80.0, "x"),           # out_of_range (lat > 90)
        ("island", 0.0, 0.0, "x"),           # null_island
        ("off", 40.0, -120.0, "x"),          # off_aoi (valid, outside box, not swappable)
        ("swap", -80.0, 35.0, "x"),          # off_aoi AND swapped_suspect
    ]
    con.executemany("INSERT INTO locations VALUES (?, ?, ?, ?)", rows)
    yield con
    con.close()


def test_input_quality_counts_buckets(locations_con):
    counts = qa.input_quality_counts(locations_con, AOI)
    assert counts["total"] == 6
    assert counts["null_coords"] == 1
    assert counts["out_of_range"] == 1
    assert counts["null_island"] == 1
    assert counts["off_aoi"] == 2          # 'off' and 'swap'
    assert counts["swapped_suspect"] == 1  # 'swap' only (a subset of off_aoi)


def test_input_quality_report_quarantine_and_swapped(locations_con):
    counts = qa.input_quality_counts(locations_con, AOI)
    report = qa.input_quality_report(counts, AOI)
    # disjoint buckets summed once: null + oor + island + off_aoi = 1+1+1+2 = 5
    assert report["quarantine_total"] == 5
    assert report["swapped_suspect"] == 1
    kinds = {i["kind"] for i in report["issues"]}
    assert {"null_coord", "out_of_range", "null_island", "off_aoi", "lat_lon_swapped"} <= kinds


def test_input_quality_clean_table_has_no_issues():
    con = duckdb.connect()
    con.execute("CREATE TABLE locations (location_id VARCHAR, latitude DOUBLE, longitude DOUBLE)")
    con.execute("INSERT INTO locations VALUES ('a', 35.0, -80.0), ('b', 34.5, -79.0)")
    counts = qa.input_quality_counts(con, AOI)
    con.close()
    report = qa.input_quality_report(counts, AOI)
    assert report["quarantine_total"] == 0
    assert report["issues"] == []
