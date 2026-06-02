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
class PipelineState:
    """Mutable state threaded through the agent graph."""

    stage: Stage = Stage.INGESTION
    total_records: int | None = None
    clean_records: int | None = None
    quality_issues: list[DataQualityIssue] = field(default_factory=list)
    findings: list[RiskFinding] = field(default_factory=list)
    # Free-form notes/log entries each agent can append for the decision log.
    notes: list[str] = field(default_factory=list)
    # Set when an agent hits an unrecoverable problem; orchestrator surfaces for intervention.
    error: str | None = None

    def log(self, message: str) -> None:
        self.notes.append(message)
