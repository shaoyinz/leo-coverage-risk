"""Shared pipeline state passed between agents.

Each agent reads the fields it needs and appends its results, so the orchestrator
can checkpoint progress and a human can inspect/intervene between stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Stage(str, Enum):
    INGESTION = "ingestion"
    DISCOVERY = "discovery"
    ANALYSIS = "analysis"
    QA = "qa"
    DONE = "done"


@dataclass
class DataQualityIssue:
    """One problem found in the source locations data."""

    kind: str  # e.g. "null_coord", "out_of_range", "duplicate", "land_in_ocean"
    count: int
    detail: str = ""


@dataclass
class RiskFinding:
    """Per-location (or per-cohort) obstruction-risk result."""

    location_id: str
    risk_score: float  # 0..1
    risk_band: str  # "low" | "medium" | "high"
    drivers: dict[str, Any] = field(default_factory=dict)  # canopy/terrain/structure contributions


@dataclass
class DatasetCandidate:
    """One dataset the discovery agent considered for an obstruction factor.

    The manifest keeps both selected and rejected candidates so the H1 reviewer can
    see *why* a layer won — the ranking criteria (resolution, vintage, AOI coverage,
    licence) are recorded in ``rationale``.
    """

    factor: str  # "terrain" | "surface" | "canopy" | "buildings"
    dataset_id: str  # collection id or off-catalog dataset name
    access: str  # "stac" | "web"
    selected: bool
    rank: int = 0
    collection: str | None = None
    catalog: str | None = None  # STAC catalog label/url, when access == "stac"
    asset_href: str | None = None  # representative asset (COG/footprint), not all tiles
    source_url: str | None = None  # registry page / S3 URI grounding an access == "web" pick
    crs: str | None = None
    gsd_m: float | None = None  # ground sample distance (resolution) in metres
    vintage: str | None = None  # acquisition date / interval
    coverage_pct: float | None = None  # share of the AOI the dataset covers
    license: str | None = None
    rationale: str = ""


@dataclass
class DataManifest:
    """A1 output: the ranked obstruction-data plan, surfaced for the H1 human gate."""

    aoi_bbox: list[float]  # [min_lon, min_lat, max_lon, max_lat]
    generated_at: str  # ISO-8601 timestamp
    entries: list[DatasetCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # licensing flags, gaps, caveats


@dataclass
class PipelineState:
    """Mutable state threaded through the agent graph."""

    stage: Stage = Stage.INGESTION
    total_records: int | None = None
    clean_records: int | None = None
    quality_issues: list[DataQualityIssue] = field(default_factory=list)
    # Ranked obstruction-data plan from the discovery agent (None until A1 runs).
    manifest: DataManifest | None = None
    findings: list[RiskFinding] = field(default_factory=list)
    # Free-form notes/log entries each agent can append for the decision log.
    notes: list[str] = field(default_factory=list)
    # Set when an agent hits an unrecoverable problem; orchestrator surfaces for intervention.
    error: str | None = None

    def log(self, message: str) -> None:
        self.notes.append(message)
