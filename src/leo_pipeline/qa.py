"""Deterministic QA / validation engine for the A4 agent.

A4's job (architecture.md §5, the A4 node) splits in two, both **deterministic** here so
the LLM only triages/explains what the rules surface and decides what reaches the H2 human
gate — it never invents a verdict:

- **Input quality** — re-audit the raw locations table for bad rows and route them to a
  quarantine queue: null/out-of-range coordinates, the (0, 0) null island, points outside
  the AOI, and lat/lon-swapped rows. (Coordinate de-dup itself is the deterministic ingest
  pre-step, ``leo_pipeline.ingest``; A4 cross-checks and quarantines.) The DuckDB query is
  ``input_quality_counts``; ``input_quality_report`` turns the counts into
  :class:`~leo_pipeline.state.DataQualityIssue` rows.
- **Output anomalies** — scan the A3 obstruction findings for results that are individually
  or collectively implausible: a region (tile or county) saturated with at-risk locations
  (the "a county at 100 % at-risk" example), a ``risk_tier`` that disagrees with its own
  ``obstruction_pct`` under the spec bands, too many ``undetermined`` / low-confidence
  scores, a degenerate all-identical distribution, or mixed ``spec_version`` in one run.
  ``run_output_qa`` runs every rule and returns a report + :class:`QAAnomaly` list.

Thresholds are the version-stamped :class:`~leo_pipeline.config.QA` spec, not magic numbers.
The engine is pure (no API, no rasters); the county join + the locations DuckDB read are
assembled by the tool/CLI layer and passed in, exactly as A3 keeps geometry in the engine
and I/O in the tool.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from leo_pipeline.config import ANALYSIS, QA
from leo_pipeline.state import DataQualityIssue, QAAnomaly

# The three "scored" tiers plus the no-datum sentinel A3 emits (state.ObstructionResult).
SCORED_TIERS = ("clear", "at_risk", "severe")
AT_RISK_TIERS = ("at_risk", "severe")
ALL_TIERS = (*SCORED_TIERS, "undetermined")

# How many offending ids to attach to an anomaly so a human can spot-check without a re-scan.
_MAX_SAMPLES = 10


def tier_for_pct(pct: float, clear_max: float, severe_min: float) -> str:
    """The tier ``obstruction_pct`` *should* carry under the spec bands.

    Mirrors the banding A3 applies (``clear`` ≤ clear_max < ``at_risk`` < severe_min ≤
    ``severe``) so QA can independently re-derive it and catch a finding whose stamped
    ``risk_tier`` disagrees with its own number — an internal-consistency bug, not a
    judgement call.
    """
    if pct <= clear_max:
        return "clear"
    if pct >= severe_min:
        return "severe"
    return "at_risk"


# --- summary -------------------------------------------------------------------------


def summarize_findings(findings: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Run-level stats over A3 findings: tier/confidence counts, coverage rates, and the
    obstruction_pct distribution over the *scored* (non-undetermined) subset."""
    n = len(findings)
    tier_counts = {t: 0 for t in ALL_TIERS}
    conf_counts: dict[str, int] = {}
    pcts: list[float] = []
    for f in findings:
        tier = f.get("risk_tier") or "undetermined"
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        conf = f.get("confidence") or "unknown"
        conf_counts[conf] = conf_counts.get(conf, 0) + 1
        pct = f.get("obstruction_pct")
        if pct is not None:
            pcts.append(float(pct))
    n_scored = sum(tier_counts[t] for t in SCORED_TIERS)
    n_undet = tier_counts.get("undetermined", 0)
    n_low = conf_counts.get("low", 0)
    return {
        "n_findings": n,
        "n_scored": n_scored,
        "tier_counts": tier_counts,
        "confidence_counts": conf_counts,
        "undetermined_rate": round(n_undet / n, 4) if n else 0.0,
        "low_confidence_rate": round(n_low / n, 4) if n else 0.0,
        "at_risk_rate": round(
            sum(tier_counts[t] for t in AT_RISK_TIERS) / n_scored, 4
        )
        if n_scored
        else 0.0,
        "obstruction_pct": _pct_distribution(pcts),
        "spec_versions": sorted({f.get("spec_version") for f in findings if f.get("spec_version")}),
    }


def _pct_distribution(pcts: list[float]) -> dict[str, Any]:
    if not pcts:
        return {"n": 0, "min": None, "max": None, "mean": None, "median": None}
    return {
        "n": len(pcts),
        "min": round(min(pcts), 3),
        "max": round(max(pcts), 3),
        "mean": round(statistics.fmean(pcts), 3),
        "median": round(statistics.median(pcts), 3),
        "n_distinct": len({round(p, 6) for p in pcts}),
    }


# --- per-location consistency / value-range rules ------------------------------------


def consistency_anomalies(
    findings: list[Mapping[str, Any]],
    *,
    clear_max: float,
    severe_min: float,
) -> list[QAAnomaly]:
    """Per-location internal-consistency rules, aggregated to one anomaly per kind.

    Catches the things a downstream consumer would trust blindly:
      - ``tier_inconsistent``  — risk_tier ≠ the tier its own obstruction_pct implies
      - ``pct_out_of_range``   — obstruction_pct outside [0, 100]
      - ``tier_pct_mismatch``  — a null pct on a scored tier, or a number on undetermined
    Each fires once with a count + a capped sample of offending location_ids, so a noisy
    run produces a few actionable anomalies rather than a million.
    """
    bad_tier: list[str] = []
    bad_range: list[str] = []
    bad_null: list[str] = []
    for f in findings:
        lid = str(f.get("location_id"))
        tier = f.get("risk_tier") or "undetermined"
        pct = f.get("obstruction_pct")
        if tier == "undetermined":
            if pct is not None:
                bad_null.append(lid)  # undetermined must not carry a number
            continue
        if pct is None:
            bad_null.append(lid)  # a scored tier must carry a number
            continue
        pct = float(pct)
        if pct < 0.0 or pct > 100.0:
            bad_range.append(lid)
            continue
        if tier_for_pct(pct, clear_max, severe_min) != tier:
            bad_tier.append(lid)

    out: list[QAAnomaly] = []
    if bad_tier:
        out.append(
            QAAnomaly(
                rule="tier_inconsistent",
                severity="critical",
                scope="run",
                detail=(
                    f"{len(bad_tier)} finding(s) carry a risk_tier that disagrees with their "
                    f"own obstruction_pct under the spec bands (clear≤{clear_max}, "
                    f"severe≥{severe_min}) — a scoring/banding bug, not a borderline call."
                ),
                metric=float(len(bad_tier)),
                threshold=0.0,
                sample_ids=bad_tier[:_MAX_SAMPLES],
            )
        )
    if bad_range:
        out.append(
            QAAnomaly(
                rule="pct_out_of_range",
                severity="critical",
                scope="run",
                detail=f"{len(bad_range)} finding(s) have obstruction_pct outside [0, 100].",
                metric=float(len(bad_range)),
                threshold=0.0,
                sample_ids=bad_range[:_MAX_SAMPLES],
            )
        )
    if bad_null:
        out.append(
            QAAnomaly(
                rule="tier_pct_mismatch",
                severity="warn",
                scope="run",
                detail=(
                    f"{len(bad_null)} finding(s) pair a null obstruction_pct with a scored "
                    "tier, or a numeric pct with 'undetermined' — tier and value disagree."
                ),
                metric=float(len(bad_null)),
                threshold=0.0,
                sample_ids=bad_null[:_MAX_SAMPLES],
            )
        )
    return out


# --- run-level coverage + drift rules ------------------------------------------------


def coverage_anomalies(summary: Mapping[str, Any], qa: QA = QA) -> list[QAAnomaly]:
    """Flag a run whose surface data is too thin to trust: too many undetermined (no datum
    under the point) or too many low-confidence scores (architecture §5 — degraded results
    stay usable but auditable, so we surface the rate rather than bury it)."""
    out: list[QAAnomaly] = []
    u_rate = summary.get("undetermined_rate", 0.0)
    if u_rate > qa.max_undetermined_rate:
        out.append(
            QAAnomaly(
                rule="undetermined_rate",
                severity="warn",
                scope="run",
                detail=(
                    f"{u_rate:.1%} of locations are undetermined (no surface under the point) "
                    f"— above the {qa.max_undetermined_rate:.0%} budget; the A2 surface "
                    "coverage is likely too sparse for this AOI."
                ),
                metric=round(float(u_rate), 4),
                threshold=qa.max_undetermined_rate,
            )
        )
    l_rate = summary.get("low_confidence_rate", 0.0)
    if l_rate > qa.max_low_confidence_rate:
        out.append(
            QAAnomaly(
                rule="low_confidence_rate",
                severity="warn",
                scope="run",
                detail=(
                    f"{l_rate:.1%} of scores are low-confidence — above the "
                    f"{qa.max_low_confidence_rate:.0%} budget; degraded/partial surfaces "
                    "dominate, so treat the run as provisional."
                ),
                metric=round(float(l_rate), 4),
                threshold=qa.max_low_confidence_rate,
            )
        )
    return out


def spec_drift_anomalies(summary: Mapping[str, Any]) -> list[QAAnomaly]:
    """More than one spec_version in a single run means the scores are not comparable —
    a reproducibility/versioning break (architecture §4)."""
    versions = summary.get("spec_versions") or []
    if len(versions) > 1:
        return [
            QAAnomaly(
                rule="spec_version_drift",
                severity="critical",
                scope="run",
                detail=(
                    f"findings span {len(versions)} obstruction-spec versions ({versions}); "
                    "scores from different specs are not directly comparable — re-score on one."
                ),
                metric=float(len(versions)),
                threshold=1.0,
            )
        ]
    return []


# --- regional (group) rules ----------------------------------------------------------


def group_findings(
    findings: list[Mapping[str, Any]], key_fn: Callable[[Mapping[str, Any]], Any]
) -> dict[Any, list[Mapping[str, Any]]]:
    """Bucket findings by an arbitrary key (tile_id, county geoid, ...). Findings whose key
    is ``None`` are dropped — e.g. a coord with no county when the maps aren't on disk."""
    groups: dict[Any, list[Mapping[str, Any]]] = {}
    for f in findings:
        key = key_fn(f)
        if key is None:
            continue
        groups.setdefault(key, []).append(f)
    return groups


def _weight(f: Mapping[str, Any], weight_of: Mapping[str, float] | None) -> float:
    """Per-finding weight for regional aggregates. Defaults to 1 (per unique coordinate);
    pass ``weight_of[location_id] = n_locations`` to weight a coord by how many real
    locations collapse onto it, so 'X % of the county' counts households, not work-items."""
    if not weight_of:
        return 1.0
    return float(weight_of.get(str(f.get("location_id")), 1.0))


def region_anomalies(
    groups: Mapping[Any, list[Mapping[str, Any]]],
    *,
    scope_prefix: str,
    qa: QA = QA,
    weight_of: Mapping[str, float] | None = None,
) -> list[QAAnomaly]:
    """Per-region rules over a grouping (tiles or counties):

    - ``saturated_region``  — ≥ ``saturated_region_rate`` of the region's *scored*
      locations are at-risk (the "a county at 100 % at-risk" smell). Weighted by
      ``weight_of`` so a county's share reflects households, not unique coordinates.
    - ``degenerate_distribution`` — a large region whose scored obstruction_pct are all
      identical (a stuck sampler / constant surface), even when the value looks benign.

    Only regions with at least ``min_region_size`` scored locations are eligible, so a
    sparsely-sampled tile edge isn't mistaken for a saturated region.
    """
    out: list[QAAnomaly] = []
    for key, items in sorted(groups.items(), key=lambda kv: str(kv[0])):
        scored = [f for f in items if (f.get("risk_tier") or "undetermined") in SCORED_TIERS]

        # Saturation rule — household-weighted, needs a region of at least min_region_size.
        n_scored_w = sum(_weight(f, weight_of) for f in scored)
        if n_scored_w >= qa.min_region_size:
            at_risk_w = sum(
                _weight(f, weight_of) for f in scored if f.get("risk_tier") in AT_RISK_TIERS
            )
            rate = at_risk_w / n_scored_w if n_scored_w else 0.0
            if rate >= qa.saturated_region_rate:
                out.append(
                    QAAnomaly(
                        rule="saturated_region",
                        severity="warn",
                        scope=f"{scope_prefix}:{key}",
                        detail=(
                            f"{rate:.0%} of {int(round(n_scored_w))} scored locations in "
                            f"{scope_prefix} {key} are at-risk (≥ {qa.saturated_region_rate:.0%}). "
                            "Implausibly uniform — verify the surface/spec before publishing, "
                            "or confirm it is genuinely a heavily-obstructed area."
                        ),
                        metric=round(float(rate), 4),
                        threshold=qa.saturated_region_rate,
                        sample_ids=[str(f.get("location_id")) for f in scored[:_MAX_SAMPLES]],
                    )
                )

        # Degenerate-distribution rule — its own gate (degenerate_group_min work-items), and
        # only when the single shared value is NON-zero. An all-zero region is the common
        # benign "open/flat, genuinely clear" case, not a stuck sampler, so we don't flag it.
        pcts = {
            round(float(f["obstruction_pct"]), 6)
            for f in scored
            if f.get("obstruction_pct") is not None
        }
        if len(scored) >= qa.degenerate_group_min and len(pcts) == 1:
            (only,) = tuple(pcts)
            if only > 0.0:
                out.append(
                    QAAnomaly(
                        rule="degenerate_distribution",
                        severity="warn",
                        scope=f"{scope_prefix}:{key}",
                        detail=(
                            f"all {len(scored)} scored locations in {scope_prefix} {key} share "
                            f"the identical non-zero obstruction_pct={only} — suspicious for a "
                            "real landscape; check for a stuck sampler or a constant surface."
                        ),
                        metric=float(len(scored)),
                        threshold=float(qa.degenerate_group_min),
                        sample_ids=[str(f.get("location_id")) for f in scored[:_MAX_SAMPLES]],
                    )
                )
    return out


# --- top-level output QA -------------------------------------------------------------


def run_output_qa(
    findings: list[Mapping[str, Any]],
    *,
    qa: QA = QA,
    clear_max: float | None = None,
    severe_min: float | None = None,
    county_of: Mapping[str, Any] | None = None,
    weight_of: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Run every output-anomaly rule and assemble the QA report.

    ``county_of[location_id] -> geoid`` enables the county grouping (built by the caller
    from the location/coord maps when present); without it only the tile grouping runs.
    ``weight_of[location_id] -> n_locations`` weights regional rates by households.
    Returns a JSON-able report; ``review_required`` is True when any anomaly fired.
    """
    clear_max = ANALYSIS.band_clear_max_pct if clear_max is None else clear_max
    severe_min = ANALYSIS.band_severe_min_pct if severe_min is None else severe_min

    summary = summarize_findings(findings)
    anomalies: list[QAAnomaly] = []
    anomalies += consistency_anomalies(findings, clear_max=clear_max, severe_min=severe_min)
    anomalies += coverage_anomalies(summary, qa)
    anomalies += spec_drift_anomalies(summary)

    tile_groups = group_findings(findings, lambda f: f.get("tile_id"))
    anomalies += region_anomalies(
        tile_groups, scope_prefix="tile", qa=qa, weight_of=weight_of
    )
    grouped_by = ["tile"]
    if county_of:
        county_groups = group_findings(findings, lambda f: county_of.get(str(f.get("location_id"))))
        anomalies += region_anomalies(
            county_groups, scope_prefix="county", qa=qa, weight_of=weight_of
        )
        grouped_by.append("county")

    severities = {a.severity for a in anomalies}
    return {
        "qa_spec_version": qa.qa_spec_version,
        "band_cutpoints_pct": {"clear_max": clear_max, "severe_min": severe_min},
        "summary": summary,
        "grouped_by": grouped_by,
        "n_anomalies": len(anomalies),
        "review_required": bool(anomalies),
        "has_critical": "critical" in severities,
        "anomalies": [vars(a) for a in anomalies],
    }


# --- input-quality audit (DuckDB) ----------------------------------------------------


def input_quality_counts(con: Any, aoi_bbox: Iterable[float]) -> dict[str, int]:
    """Audit the raw ``locations`` table for quarantine-worthy rows, in one DuckDB pass.

    ``con`` is a connection from :func:`leo_pipeline.ingest.connect` (the CSV exposed as
    ``locations``); ``aoi_bbox`` is ``[min_lon, min_lat, max_lon, max_lat]``. Returns raw
    counts (not rows) so the ~4.7M-row table is never materialised — A4 reports the size of
    each quarantine bucket, the deterministic ingest step does the actual fan-out.

    Buckets (mirrors architecture §5 "bad input row"):
      - ``null_coords``       — latitude or longitude is NULL
      - ``out_of_range``      — lat∉[-90,90] or lon∉[-180,180]
      - ``null_island``       — exactly (0, 0)
      - ``off_aoi``           — valid coordinate, but outside the AOI box
      - ``swapped_suspect``   — outside the AOI as given, but inside it if lat/lon swap
    ``swapped_suspect`` is a subset of ``off_aoi`` (a likely-recoverable cause), reported
    separately so a human can decide whether to fix-and-requeue rather than drop.
    """
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in aoi_bbox)
    row = con.execute(
        """
        SELECT
            count(*),
            count(*) FILTER (WHERE latitude IS NULL OR longitude IS NULL),
            count(*) FILTER (
                WHERE latitude NOT BETWEEN -90 AND 90
                   OR longitude NOT BETWEEN -180 AND 180),
            count(*) FILTER (WHERE latitude = 0 AND longitude = 0),
            count(*) FILTER (
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                  AND latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180
                  AND NOT (latitude = 0 AND longitude = 0)
                  AND NOT (longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ?)),
            count(*) FILTER (
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                  AND NOT (longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ?)
                  AND (latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?)),
            count(DISTINCT (latitude, longitude))
        FROM locations
        """,
        [
            min_lon, max_lon, min_lat, max_lat,  # off_aoi: not inside box as given
            min_lon, max_lon, min_lat, max_lat,  # swapped: not inside as given ...
            min_lon, max_lon, min_lat, max_lat,  # ... but inside if lat<->lon swapped
        ],
    ).fetchone()
    keys = (
        "total",
        "null_coords",
        "out_of_range",
        "null_island",
        "off_aoi",
        "swapped_suspect",
        "distinct_coords",
    )
    return dict(zip(keys, (int(v) for v in row)))


def input_quality_report(
    counts: Mapping[str, int], aoi_bbox: Iterable[float]
) -> dict[str, Any]:
    """Turn :func:`input_quality_counts` into DataQualityIssue rows + a quarantine summary.

    The quarantine total counts null / out-of-range / null-island / off-AOI rows once each
    (the buckets are disjoint by construction); ``swapped_suspect`` is a recoverable subset
    of ``off_aoi`` and is reported as a note, not added again."""
    bbox = [float(v) for v in aoi_bbox]
    rules = [
        ("null_coord", counts.get("null_coords", 0), "latitude or longitude is NULL"),
        ("out_of_range", counts.get("out_of_range", 0), "lat∉[-90,90] or lon∉[-180,180]"),
        ("null_island", counts.get("null_island", 0), "coordinate is exactly (0, 0)"),
        ("off_aoi", counts.get("off_aoi", 0), f"valid coordinate outside the AOI {bbox}"),
    ]
    issues = [
        DataQualityIssue(kind=kind, count=n, detail=detail)
        for kind, n, detail in rules
        if n > 0
    ]
    swapped = counts.get("swapped_suspect", 0)
    if swapped > 0:
        issues.append(
            DataQualityIssue(
                kind="lat_lon_swapped",
                count=swapped,
                detail=(
                    "outside the AOI as given but inside it if lat/lon are swapped — a "
                    "subset of off_aoi; likely recoverable by swapping rather than dropping"
                ),
            )
        )
    total = counts.get("total", 0)
    quarantine = sum(n for _, n, _ in rules)
    return {
        "total_rows": total,
        "distinct_coords": counts.get("distinct_coords", 0),
        "quarantine_total": quarantine,
        "quarantine_rate": round(quarantine / total, 6) if total else 0.0,
        "swapped_suspect": swapped,
        "issues": [vars(i) for i in issues],
    }
