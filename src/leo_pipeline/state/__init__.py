"""Shared pipeline state passed between agents.

Each agent reads the fields it needs and appends its results, so the orchestrator
can checkpoint progress and a human can inspect/intervene between stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Stage(str, Enum):
    DISCOVERY = "discovery"  # A1: AOI + spec -> ranked data manifest (H1 gate)
    INGESTION = "ingestion"  # A2: COG windows per tile -> aligned pseudo-DSM surfaces
    ANALYSIS = "analysis"  # A3: horizon profile -> obstruction% -> risk tier
    QA = "qa"  # A4: output anomaly checks
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
class SurfaceTile:
    """A2 output: one aligned elevation surface for a tile.

    A reference to a cached raster (``dsm_uri``), never the raster itself — context carries
    file URIs, not pixels (architecture §4). ``surface_mode`` records where the tile landed
    in the fallback hierarchy and ``confidence``/``coverage_flag`` carry how trustworthy the
    surface is, so a degraded tile stays usable and auditable rather than silently wrong.
    """

    tile_id: str
    bbox: list[float]  # [min_lon, min_lat, max_lon, max_lat] (EPSG:4326)
    dsm_uri: str  # cached pseudo-DSM / true-DSM GeoTIFF
    dem_uri: str | None = None  # cached bare-earth DEM (pseudo_dsm only)
    crs: str | None = None  # metric CRS the surface is in (e.g. EPSG:32617)
    gsd_m: float | None = None  # output resolution
    surface_mode: str = ""  # "true_dsm" | "pseudo_dsm" | "cover_proxy"
    vintage_map: dict[str, Any] = field(default_factory=dict)  # factor -> dataset vintage
    coverage_flag: str = ""  # "ok" | "partial" | "empty" | "cover_proxy"
    confidence: str = ""  # "high" | "medium" | "low"


@dataclass
class ObstructionResult:
    """A3 output: one location's sky-obstruction score against an aligned surface.

    Carries the dwell-time-weighted ``obstruction_pct``, the three-state ``risk_tier``
    (``clear`` | ``at_risk`` | ``severe`` | ``undetermined``), and a ``confidence`` flag so a
    degraded or borderline result stays usable and auditable rather than silently wrong
    (architecture.md §5). ``spec_version`` stamps which approved obstruction spec produced the
    score, so it is reproducible and drift-detectable (architecture.md §4)."""

    location_id: str
    obstruction_pct: float | None  # dwell-weighted % of usable sky removed (None = undetermined)
    risk_tier: str  # "clear" | "at_risk" | "severe" | "undetermined"
    confidence: str  # "high" | "medium" | "low"
    dish_height_m: float = 0.0  # mount height above the surface this score assumed
    blocked_azimuths: list[float] = field(default_factory=list)
    tile_id: str | None = None
    dsm_uri: str | None = None  # the A2 surface scored against (a reference, not pixels)
    spec_version: str = ""  # the obstruction spec version the score was computed under


@dataclass
class PipelineState:
    """Mutable state threaded through the agent graph."""

    stage: Stage = Stage.DISCOVERY
    total_records: int | None = None
    clean_records: int | None = None
    quality_issues: list[DataQualityIssue] = field(default_factory=list)
    # Ranked obstruction-data plan from the discovery agent (None until A1 runs).
    manifest: DataManifest | None = None
    # Aligned per-tile surfaces from the ingestion agent (A2; empty until A2 runs).
    surfaces: list[SurfaceTile] = field(default_factory=list)
    # Per-location obstruction scores from the analysis agent (A3; empty until A3 runs).
    obstruction: list[ObstructionResult] = field(default_factory=list)
    findings: list[RiskFinding] = field(default_factory=list)
    # Free-form notes/log entries each agent can append for the decision log.
    notes: list[str] = field(default_factory=list)
    # Set when an agent hits an unrecoverable problem; orchestrator surfaces for intervention.
    error: str | None = None

    def log(self, message: str) -> None:
        self.notes.append(message)
