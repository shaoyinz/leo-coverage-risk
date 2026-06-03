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

    # STAC catalog endpoints the agent may query (label -> root URL). Planetary Computer
    # is the primary; Earth Search (Element84, AWS-hosted) is a second general STAC the
    # agent can probe for collections PC lacks. NOTE: tree-canopy *height* is not on any of
    # these (Meta's "dataforgood-fb-forests" publishes no STAC endpoint — verified), which
    # is why canopy is sourced via ``opendata_registry_search`` / ``web_sources`` instead.
    stac_catalogs: dict[str, str] = field(
        default_factory=lambda: {
            "planetary_computer": "https://planetarycomputer.microsoft.com/api/stac/v1",
            "earth_search": "https://earth-search.aws.element84.com/v1",
        }
    )
    # Obstruction factor -> candidate STAC collection ids (best first). ``canopy`` is
    # intentionally empty: no STAC catalog hosts canopy *height*, so it is sourced
    # off-catalog via ``opendata_registry_search`` + ``web_sources`` (verify, don't guess).
    candidate_collections: dict[str, list[str]] = field(
        default_factory=lambda: {
            "terrain": ["3dep-seamless", "cop-dem-glo-30", "nasadem"],
            "surface": ["3dep-lidar-dsm"],
            "buildings": ["ms-buildings"],
            "canopy": [],
        }
    )
    # Curated off-catalog sources, keyed by obstruction factor. This is a vetted snapshot of
    # AWS Open Data Registry (and other open) datasets so the discovery agent can *verify* a
    # known-good candidate rather than *discover* one from a blank web search — the web path
    # is unreliable (general search, US-only, sometimes disabled at the org level), which is
    # how a well-known dataset like Meta canopy height gets missed. ``registry_id`` is the
    # awslabs/open-data-registry YAML basename used by ``opendata_registry_search`` to fetch
    # and verify the live record; ``None`` means the source is not in that registry.
    web_sources: dict[str, list[dict]] = field(
        default_factory=lambda: {
            "canopy": [
                {
                    "name": "High Resolution Canopy Height Maps by WRI and Meta",
                    "registry_id": "dataforgood-fb-forests",
                    "registry_url": "https://registry.opendata.aws/dataforgood-fb-forests/",
                    "s3_uri": "s3://dataforgood-fb-data/forests/v1/alsgedi_global_v6_float/",
                    "gsd_m": 1.0,
                    "vintage": "2009-2020 (Maxar imagery epoch)",
                    "license": "CC-BY-4.0",
                    "description": "Global ~1 m tree-canopy HEIGHT from ML on Maxar imagery.",
                    "keywords": ["canopy", "height", "tree", "forest", "chm", "vegetation"],
                },
                {
                    "name": "ETH Global Sentinel-2 10m Canopy Height 2020",
                    "registry_id": None,  # Zenodo / Google Earth Engine, not the AWS registry
                    "registry_url": "https://langnico.github.io/globalcanopyheight/",
                    "s3_uri": None,
                    "gsd_m": 10.0,
                    "vintage": "2020",
                    "license": "CC-BY-4.0",
                    "description": "Global 10 m canopy height (ETH Zurich); coarser alternative.",
                    "keywords": ["canopy", "height", "tree", "forest", "eth", "sentinel"],
                },
            ],
            "buildings": [
                {
                    "name": "Overture Maps Foundation",
                    "registry_id": "overturemaps",
                    "registry_url": "https://registry.opendata.aws/overturemaps/",
                    "s3_uri": "s3://overturemaps-us-west-2/release/",
                    "gsd_m": None,
                    "vintage": "rolling",
                    "license": "CDLA-Permissive-2.0",
                    "description": "Global building footprints, many with HEIGHTS (vector).",
                    "keywords": ["building", "footprint", "height", "overture", "structure"],
                },
            ],
        }
    )
    # Where write_data_manifest persists the H1 review artifact.
    manifest_path: Path = REPO_ROOT / "data" / "interim" / "data_manifest.json"
    # Fallback AOI (CONUS bbox, lon/lat) used only when the locations CSV is absent.
    default_aoi_bbox: tuple[float, float, float, float] = (-125.0, 24.0, -66.5, 49.5)


@dataclass(frozen=True)
class Ingestion:
    """Config for the A2 surface-ingestion agent: tiling + COG fetch/align/fuse.

    A2 turns the H1-approved data manifest into one aligned surface per tile. Tiles are
    the unit of both the windowed COG fetch and the LLM batch, which is what keeps LLM
    cost ``O(tiles)`` while the geospatial compute stays ``O(locations)`` (architecture
    §6). The defaults below are the knobs the ingestion stage exposes; the geometry knobs
    (θ, σ_H, ...) live in the obstruction spec consumed by A3, not here.
    """

    # Precise-pass tile edge length, in metres of the AOI's UTM CRS. The grid is built in
    # UTM so a tile is a real square on the ground rather than a lat/lon trapezoid.
    tile_size_m: float = 5000.0
    # Overlap buffer added around every tile's fetch window so an obstacle sitting just
    # outside a tile edge (a tall tree/ridge) still enters the surface and isn't lost at
    # the seam (architecture §5, "tile-edge object"). >= the near-field terrain radius.
    tile_buffer_m: float = 300.0
    # Common resample grid the COG windows are reprojected onto before fusion
    # (architecture §3 fetch_aligned_surface default). Finer captures smaller obstacles at
    # higher cost; see docs/rationale.md `raster_resolution_m`.
    target_gsd_m: float = 10.0
    # Surface fallback hierarchy, best first (docs/rationale.md `surface_source_preference`,
    # architecture §5 download-error row). A2 walks DOWN this on a read failure: a true
    # lidar DSM is ideal, else fuse DEM + max(canopy, building) into a pseudo-DSM, else a
    # coarse cover proxy with lowered confidence.
    surface_modes: tuple[str, ...] = ("true_dsm", "pseudo_dsm", "cover_proxy")
    # Tile-keyed, content-addressed cache of aligned surfaces. Re-running a tile with
    # unchanged inputs is a no-op (architecture §4 idempotency → resume / cost control).
    cache_dir: Path = REPO_ROOT / "data" / "interim" / "surfaces"
    # Sign Planetary Computer asset hrefs at download time (the unsigned hrefs A1 persists
    # in the manifest are short-lived to sign, so signing belongs here in A2, not A1).
    sign_assets: bool = True


PATHS = Paths()
MODELS = Models()
DISCOVERY = Discovery()
INGESTION = Ingestion()


def require_api_key() -> str:
    """Return the Anthropic API key or raise a clear error if unset."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return key
