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
    # architecture §5 download-error row). A2 walks DOWN this on a read failure. ``mosaic`` is
    # the best when the manifest carries BOTH a lidar DSM and a DEM: lidar where present, DEM
    # filling the holes (per-pixel provenance band) so coverage is complete; else a single
    # true lidar DSM, else DEM + max(canopy, building) fused into a pseudo-DSM, else a coarse
    # DEM-only cover proxy with lowered confidence.
    surface_modes: tuple[str, ...] = ("mosaic", "true_dsm", "pseudo_dsm", "cover_proxy")
    # Tile-keyed, content-addressed cache of aligned surfaces. Re-running a tile with
    # unchanged inputs is a no-op (architecture §4 idempotency → resume / cost control).
    cache_dir: Path = REPO_ROOT / "data" / "interim" / "surfaces"
    # Sign Planetary Computer asset hrefs at download time (the unsigned hrefs A1 persists
    # in the manifest are short-lived to sign, so signing belongs here in A2, not A1).
    sign_assets: bool = True


@dataclass(frozen=True)
class Analysis:
    """The obstruction SPEC for the A3 analysis agent — a pre-approved, version-stamped
    config, NOT generated at runtime (architecture.md §2, the ``SPEC`` node).

    The domain research is already complete (docs/rationale.md), so the reception geometry
    (θ_min, the azimuth-weighting gradient) and the risk-tier cut-points enter the run as a
    frozen config tied to one ``spec_version`` — which is what lets every score trace back to
    a single spec and drops a runtime agent + a human gate. Every knob here is a documented
    entry in the rationale's "Tuneable parameters" registry; the defaults match that doc.
    """

    # Version stamp recorded on every finding so a score is reproducible and drift-detectable
    # (architecture.md §4 versioning). Bump when any geometry/threshold default below changes.
    spec_version: str = "spec-2026.06-theta25"

    # --- Reception geometry (physical / regulatory; rationale "Starlink reception geometry") ---
    # Minimum reception (elevation) angle θ above the horizon. 25° is the conservative,
    # app-documented default; re-scorable to 10–20° per the Apr-2026 FCC rules (a lower θ
    # widens the cone AND grows the search radius d_max ∝ 1/tanθ — a knob, not a free win).
    min_elevation_deg: float = 25.0
    # Half-angle of the dish's required sky cone from boresight (Gen3 standard 110° full cone).
    # The required sky region is the azimuth arc within this half-width of the pointing bearing.
    cone_half_angle_deg: float = 55.0
    # Boresight bearing the azimuth weighting is centred on (degrees from north, clockwise).
    # North-ish in CONUS; per-site the Starlink app reports the optimum, so it's a parameter.
    pointing_azimuth_deg: float = 0.0
    # Azimuth weighting profile: 'uniform' (omni, conservative), 'north_biased' (static
    # gradient heaviest toward the pointing bearing, lightest in the southern GSO keep-out
    # band), or 'tle_derived' (v2 dwell-time map — not yet implemented).
    az_weighting: str = "north_biased"
    # Half-width of the GSO-arc keep-out band in the southern sky where weighting is suppressed.
    gso_keepout_halfwidth_deg: float = 18.0

    # --- Install / siting ---
    # Dish mount heights above the surface to sweep (metres). Roof-only (0) is the default; the
    # sweep lets A3 report "clear if raised to X m" instead of hard-coding one pole height.
    mount_heights_m: tuple[float, ...] = (0.0, 1.5, 3.0)

    # --- Risk banding (rationale "Risk banding & uncertainty") ---
    # obstruction_pct (dwell-weighted % of usable sky removed) cut-points for the tiers:
    # ≤ clear_max → 'clear' 🟢; < severe_min → 'at_risk' 🟡; ≥ severe_min → 'severe' 🔴.
    # MUST be calibrated against the Starlink app on a labelled sample (open question).
    band_clear_max_pct: float = 1.0
    band_severe_min_pct: float = 10.0
    # Vertical-uncertainty budget (metres). In this first live build σ_H is folded into the
    # confidence flag (a borderline pct near a cut-point is reported lower-confidence) rather
    # than a full probabilistic clearance-margin model — the latter is a documented next step.
    sigma_h_m: float = 3.0

    # --- Search & computation ---
    # Angular resolution of the horizon profile (degrees). Finer = more accurate, more compute.
    azimuth_step_deg: float = 2.0
    # Outer query bound (metres) for the per-azimuth ray march. A safety clamp on cost; the
    # exact per-feature cutoff d_max = (H_b − H_a)/tanθ still governs whether a sample blocks,
    # so this only ever *tightens* the physically-derived radius (rationale per-class table).
    max_radius_m: float = 1500.0
    # Earth-curvature + refraction correction. Off near-field (sub-cm drop at few-hundred-m
    # obstacle scale); engage only for the km-scale terrain horizon pass.
    earth_curvature: bool = False

    # Where run_analysis persists per-location obstruction findings.
    findings_dir: Path = REPO_ROOT / "data" / "interim" / "analysis"


@dataclass(frozen=True)
class QA:
    """The QA SPEC for the A4 validation/QA agent — a pre-approved, version-stamped
    config of anomaly thresholds, NOT generated at runtime (mirrors ``Analysis``).

    A4 has two deterministic checks (architecture.md §5, the A4 node):
      - **input quality** over the raw locations table (bad rows → a quarantine queue:
        null/out-of-range coordinates, the (0,0) null island, points outside the AOI,
        and lat/lon-swapped rows). Coordinate de-dup itself is the deterministic ingest
        pre-step (``leo_pipeline.ingest``); A4 re-audits and quarantines.
      - **output anomalies** over the A3 obstruction findings: a region implausibly
        saturated with at-risk locations (the "a county at 100 % at-risk" example), a
        risk_tier that disagrees with its own ``obstruction_pct`` under the spec bands,
        too many ``undetermined`` / low-confidence scores, a degenerate (all-identical)
        distribution, or mixed ``spec_version`` across one run.
    The code computes the stats + fires these rules; the LLM only triages/explains the
    flagged anomalies and decides whether to route them to the H2 human gate.
    """

    # Version stamp recorded on every QA report so an audit is reproducible (architecture
    # §4 versioning). Bump when any threshold below changes.
    qa_spec_version: str = "qa-2026.06-v1"

    # --- input-quality / quarantine ---
    # Default AOI (the confirmed Carolinas/N-Georgia box) used to flag off-AOI and
    # lat/lon-swapped rows when no AOI is supplied by the caller (manifest / CSV bbox).
    aoi_bbox: tuple[float, float, float, float] = (-84.32, 33.84, -75.46, 36.59)

    # --- output-anomaly thresholds ---
    # A region (tile or county) is flagged when at least this share of its scored
    # locations land in an at-risk tier (at_risk|severe) — the "100 % at-risk" smell.
    saturated_region_rate: float = 0.95
    # Don't call a handful of points a "region": only group with at least this many
    # scored locations is eligible for the saturation / degenerate-distribution rules.
    min_region_size: int = 30
    # A group of at least this many scored locations whose obstruction_pct are all identical
    # AND non-zero is suspicious (a stuck sampler / constant surface). An all-zero region is
    # the common benign "open/flat, genuinely clear" case, so it is deliberately not flagged.
    degenerate_group_min: int = 20
    # Run-level coverage rules: flag when the share of undetermined (no surface under the
    # point) or low-confidence scores exceeds these — the surface data is too thin to trust.
    max_undetermined_rate: float = 0.20
    max_low_confidence_rate: float = 0.30

    # Where run_qa persists the QA report (the H2 review artifact).
    reports_dir: Path = REPO_ROOT / "data" / "interim" / "qa"


@dataclass(frozen=True)
class Reporting:
    """Config for the A5 reporting/insight agent — aggregates, the interactive map, and the
    officer-facing decision log (architecture.md §2, the A5 node; §7 H2 insight sign-off).

    A5 turns the A3 obstruction findings (and the A4 anomalies) into the three things a state
    broadband officer actually consumes: a **county + state aggregate** of who is at risk
    (household-weighted by ``n_locations`` via the dedup map), an **interactive map** (a
    per-location point layer coloured by risk tier, served as PMTiles + a self-contained
    MapLibre viewer so it scales to ~1M points with no server), and a **plain-English
    decision log**. The aggregation + map tiles are deterministic code (``leo_pipeline.report``);
    the LLM only writes the narrative. Version-stamped so a published report is reproducible.
    """

    # Stamp recorded on every report so a published artifact is reproducible (architecture §4).
    report_version: str = "report-2026.06-v1"
    # Where A5 writes its artifacts (map HTML/PMTiles, GeoJSON, decision-log markdown + JSON).
    outputs_dir: Path = REPO_ROOT / "outputs"

    # How many highest-risk counties to spotlight in the decision log's priority table
    # (the full ranking is always in the JSON sidecar).
    top_n_counties: int = 15

    # Risk-tier -> hex colour, shared by the map layer and the report legend. Matches the
    # 🟢/🟡/🔴/⚪ banding in docs/rationale.md (clear / at-risk / severe / undetermined).
    tier_colors: dict[str, str] = field(
        default_factory=lambda: {
            "clear": "#1a9850",
            "at_risk": "#fdae61",
            "severe": "#d73027",
            "undetermined": "#999999",
        }
    )

    # Vector-tile zoom range for tippecanoe (point layer). z3 ≈ CONUS, z12 ≈ neighbourhood —
    # enough to zoom from the AOI overview down to individual at-risk clusters.
    map_min_zoom: int = 3
    map_max_zoom: int = 12
    # Layer name embedded in the PMTiles + referenced by the MapLibre style.
    map_layer_name: str = "locations"
    # Base raster tiles for the MapLibre viewer (no API key; OSM standard tiles).
    map_basemap_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


PATHS = Paths()
MODELS = Models()
DISCOVERY = Discovery()
INGESTION = Ingestion()
ANALYSIS = Analysis()
QA = QA()
REPORTING = Reporting()


def require_api_key() -> str:
    """Return the Anthropic API key or raise a clear error if unset."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return key
