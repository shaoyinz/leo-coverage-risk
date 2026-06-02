"""Central configuration: model selection, repo paths, environment loading.

Kept dependency-light so it can be imported from notebooks, tools, and the
orchestrator without side effects beyond loading ``.env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Repo root = two levels up from this file (src/leo_pipeline/config.py -> repo).
REPO_ROOT = Path(__file__).resolve().parents[2]

# Load .env from the repo root if present (no error if missing).
load_dotenv(REPO_ROOT / ".env")


@dataclass(frozen=True)
class Paths:
    root: Path = REPO_ROOT
    data_raw: Path = REPO_ROOT / "data" / "raw"
    data_interim: Path = REPO_ROOT / "data" / "interim"
    outputs: Path = REPO_ROOT / "outputs"
    resources: Path = REPO_ROOT / "resources"
    docs: Path = REPO_ROOT / "docs"
    # Expected provided inputs (see data/raw/README.md).
    locations_csv: Path = REPO_ROOT / "data" / "raw" / "DATA_CHALLENGE_50.csv"
    install_guide_pdf: Path = REPO_ROOT / "resources" / "starlink_install_guide.pdf"


@dataclass(frozen=True)
class Models:
    # Opus drives orchestration / planning; Sonnet handles higher-volume worker calls.
    driver: str = os.getenv("LEO_DRIVER_MODEL", "claude-opus-4-8")
    worker: str = os.getenv("LEO_WORKER_MODEL", "claude-sonnet-4-6")


PATHS = Paths()
MODELS = Models()


def require_api_key() -> str:
    """Return the Anthropic API key or raise a clear error if unset."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return key
