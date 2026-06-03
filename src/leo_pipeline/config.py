"""Central configuration: model selection, repo paths, environment loading.

Kept dependency-light so it can be imported from notebooks, tools, and the
orchestrator without side effects beyond loading ``.env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Discovery:
    """Config for the A1 data-discovery agent: which catalogs/collections to search.

    The agent searches these STAC collections for the obstruction surfaces the
    methodology needs (terrain DEM, lidar DSM, building footprints), ranks the hits,
    and writes a manifest. Tree-canopy *height* is deliberately absent from the
    candidate list because no Planetary Computer collection hosts it — the agent is
    expected to source it off-catalog via web research (Meta 1 m / ETH 10 m), which is
    why ``candidate_collections["canopy"]`` is empty rather than missing.
    """

    # STAC catalog endpoints the agent may query (label -> root URL).
    stac_catalogs: dict[str, str] = field(
        default_factory=lambda: {
            "planetary_computer": "https://planetarycomputer.microsoft.com/api/stac/v1",
        }
    )
    # Obstruction factor -> candidate STAC collection ids (best first). ``canopy`` is
    # intentionally empty: off-catalog, sourced via WebSearch/WebFetch.
    candidate_collections: dict[str, list[str]] = field(
        default_factory=lambda: {
            "terrain": ["3dep-seamless", "cop-dem-glo-30", "nasadem"],
            "surface": ["3dep-lidar-dsm"],
            "buildings": ["ms-buildings"],
            "canopy": [],
        }
    )
    # Where write_data_manifest persists the H1 review artifact.
    manifest_path: Path = REPO_ROOT / "data" / "interim" / "data_manifest.json"
    # Fallback AOI (CONUS bbox, lon/lat) used only when the locations CSV is absent.
    default_aoi_bbox: tuple[float, float, float, float] = (-125.0, 24.0, -66.5, 49.5)


PATHS = Paths()
MODELS = Models()
DISCOVERY = Discovery()


def require_api_key() -> str:
    """Return the Anthropic API key or raise a clear error if unset."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return key
