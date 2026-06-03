# LEO Satellite Coverage Risk Analysis

An **agent-driven geospatial data pipeline** that identifies locations inside a LEO
satellite provider's service footprint whose connectivity is likely degraded by
environmental obstructions — tree canopy, terrain, and structures.

Submission for the Ready Builders Challenge
([issue #50](https://github.com/ready/builders-challenge/issues/50)).

## Status

Methodology, Starlink reception geometry, and tuneable parameters documented
(`docs/rationale.md`); de-duplication + tiling, the **A1 data-discovery agent** (live STAC
search → ranked data manifest), and the **A2 surface-ingestion agent** (live windowed COG
read → reproject → fused pseudo-DSM per tile), and the **A3 analysis agent** (live
per-azimuth horizon profile → dwell-weighted obstruction % → `clear`/`at_risk`/`severe`/
`undetermined` tier, plus a "find a clearer spot / mast height" search) are all live, with
standalone `leo-discovery` / `leo-tiling` / `leo-ingestion` / `leo-analysis` CLIs and a
deterministic `tests/` suite (offline; opt-in live tests). A3's `leo-analysis --compute`
runs the same horizon engine without an API call. Remaining: reporting/insight (A5). See the
[decision log](#decision-log).

## Layout

```
docs/        architecture, rationale, data-sources (+ Mermaid diagram)
src/leo_pipeline/
  config.py        models, paths, env loading; Discovery (A1) + Ingestion (A2) + Analysis (A3) knobs
  orchestrator.py  wires tools + agents into Claude Agent SDK options
  ingest.py        deterministic input profiling + coordinate de-duplication (pre-step)
  tiling.py        deterministic UTM tiling of the unique-coordinate work list (pre-step)
  horizon.py       deterministic per-azimuth horizon / obstruction engine (A3 core)
  run_discovery.py / run_ingestion.py / run_analysis.py   standalone A1 / A2 / A3 CLIs
  agents/          ingestion (A2) · data-discovery (A1) · geo-analysis (A3) · qa (least-privilege)
  tools/           @tool defs + in-process SDK MCP server ("leo")
  state/           PipelineState (+ SurfaceTile, ObstructionResult) threaded between agents
notebooks/   00_data_inspection.ipynb — first-pass CSV profiling
data/raw/    provided locations.csv (gitignored; supplied by the challenge)
data/interim/  de-dup work list, tiles, data_manifest.json, surfaces/ cache, analysis/ findings (gitignored)
resources/   Starlink Install Guide PDF (supplied by the challenge)
outputs/     generated maps / figures (gitignored)
```

## Setup

Uses the shared workspace venv at `../../.venv` (Python 3.12).

```bash
../../.venv/bin/pip install -e ".[viz,dev]"   # deps (geo stack already present)
cp .env.example .env                           # then paste your ANTHROPIC_API_KEY
```

Provided resources are **not** in the repo — drop them in:
- `data/raw/locations.csv` (challenge attachment; see `data/raw/README.md`)
- `resources/starlink_install_guide.pdf` (challenge attachment)

The `ANTHROPIC_API_KEY` (challenge testing budget) goes in `.env`, never committed.

## Run

```bash
../../.venv/bin/python -m leo_pipeline.orchestrator        # print wired config (no API calls)
../../.venv/bin/python -m leo_pipeline.orchestrator --run  # drive agents (needs ANTHROPIC_API_KEY)
```

Run the **A1 data-discovery agent on its own** to produce a ranked manifest, with an
optional custom AOI (no locations CSV required):

```bash
../../.venv/bin/python -m leo_pipeline.run_discovery                        # AOI from the locations CSV
../../.venv/bin/python -m leo_pipeline.run_discovery \
    --bbox -80 35 -78.5 35.1 --out /tmp/manifest.json                       # custom AOI → chosen path
../../.venv/bin/python -m leo_pipeline.run_discovery --bbox -80 35 -78.5 35.1 --dry-run  # resolve config, no API call
```

When a `--bbox` AOI is supplied and the locations CSV is present, the agent reports how
many of its points fall **inside** that box (not just the box itself).

Tile the de-duplicated work list, then run the **A2 surface-ingestion agent** to build one
aligned pseudo-DSM (or true lidar DSM) per tile from the H1-approved manifest:

```bash
../../.venv/bin/python -m leo_pipeline.tiling                              # unique coords → UTM tiles
../../.venv/bin/python -m leo_pipeline.run_ingestion --limit 3 --dry-run   # resolve manifest+tiles, no API
../../.venv/bin/python -m leo_pipeline.run_ingestion --limit 3             # drive A2 (needs ANTHROPIC_API_KEY)
../../.venv/bin/python -m leo_pipeline.run_ingestion --bbox -80 35 -79.95 35.05  # ingest an ad-hoc area
```

A2 reads only COG **windows** for each tile, reprojects to the tile's UTM zone, and fuses
`DEM + max(canopy, building)` into a pseudo-DSM (or passes a true lidar DSM through),
writing a tile-keyed, content-addressed cache under `data/interim/surfaces/` — re-running a
tile with unchanged inputs is a no-op. (Building-height fusion is a documented deferred gap;
pseudo-DSM currently fuses DEM + canopy.)

Tests (offline by default; the live COG-read + live agent tests are gated on `LEO_RUN_LIVE=1`):

```bash
../../.venv/bin/python -m pytest                                  # deterministic unit + contract tests
LEO_RUN_LIVE=1 ../../.venv/bin/python -m pytest -m live -k ingestion   # real DEM window read (no API key)
```

## Step-0 pre-work (from the Install Guide)

> Captured in `docs/rationale.md` (methodology, reception geometry, assumptions).

1. **Physical conditions causing service interruptions** — obstacles (tree canopy,
   terrain, buildings/structures) that rise above the dish's minimum reception line
   and block its sky-view cone.
2. **Environmental requirements for dish connectivity** — a roof-mounted dish needs a
   clear view above the minimum elevation angle θ (default 25°, FCC-relaxed toward
   10–20° in 2026) across its azimuth/FOV cone (~110° on Gen3), aimed at the assigned
   constellation region (north-ish in CONUS). Per-obstacle clear-view kernel:
   `H_b − H_a < Dist_ab · tan θ`.
3. **Public geospatial datasets to model these at scale** — DEM, DSM/nDSM height
   surface, canopy-height raster, building footprints (+ a second corroborating
   source), and parcels (for parcel-clipped alternative-site suggestions). See
   `docs/data-sources.md`.
4. **Limitations of remote analysis** — partly captured in the rationale's assumptions
   (conservative single-epoch surfaces, leaf-on canopy, roof-only default height);
   full write-up pending under "Known limitations" (seasonal foliage, fine-scale
   obstructions, exact mount height/placement, data currency/resolution).

## Deliverables map

| Deliverable | Where |
|-------------|-------|
| Agent system design & architecture diagram | `docs/architecture.md`, `docs/diagrams/architecture.mmd` |
| Tool definitions with schemas | `src/leo_pipeline/tools/` |
| State management between agents | `src/leo_pipeline/state/` |
| Analysis rationale & methodology | `docs/rationale.md` |
| Data sourcing & quality | `docs/data-sources.md`, `notebooks/00_data_inspection.ipynb`; live `data-discovery` agent + `stac_search`/`write_data_manifest` (`src/leo_pipeline/`) |
| Search / download agents (live) | A1 `data-discovery` (`stac_search`) + A2 `ingestion` (`stac_item_read`/`fetch_aligned_surface`/`cache_rw`), `src/leo_pipeline/` |
| AI-tool disclosure | `AI_TOOLS.md` |

## Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-06-03 | **Build the A3 analysis agent** — live per-azimuth horizon profile → obstruction % → risk tier | Wired the analytical core into `src/`: a deterministic `horizon.py` engine (bilinear surface sampler + ray-marched per-azimuth horizon `H(φ) = max_r arctan((Z_surface − Z_dish)/r)`, dwell-time-weighted blocked-sky fraction over the dish's azimuth cone, three-state tiering) and the two architecture A3 tools — `compute_sky_obstruction` (batch-per-tile scoring → `obstruction_pct`, `blocked_azimuths`, `clear`/`at_risk`/`severe`/`undetermined` tier, confidence) and `find_clear_sky_spot` (grid + mast-height search for a lower-obstruction position, scenario 3). Added an `Analysis` **version-stamped obstruction spec** (the pre-approved θ/cone/azimuth-weighting/band config — `SPEC` node, not generated at runtime), an `ObstructionResult` state record, and a standalone `leo-analysis` CLI with a no-API `--compute` path that runs the same engine directly. The LLM only picks parameters (mount-height sweep, relaxing θ) and handles edges; all geometry is deterministic and vectorized. **Replaced** the placeholder `lookup_obstruction_layer`/`compute_risk_score` stubs with these real tools. **Deferred:** σ_H folded into the confidence flag rather than a full probabilistic clearance-margin model, and `az_weighting='tle_derived'` falls back to the static north-biased gradient. Added 25 offline tests (real-GeoTIFF surfaces). **Not yet agentically verified** (no live A3 agent run); the deterministic engine is verified offline. |
| 2026-06-03 | **Build the A2 surface-ingestion agent** — live windowed COG read → reproject → fused pseudo-DSM per tile | Wired the second "search/download" role into `src/`: a new `ingestion` agent (A2) with three least-privilege tools — `stac_item_read` (resolve an approved collection to a signed, download-ready COG for a tile), `fetch_aligned_surface` (windowed `rasterio` read + reproject to the tile's UTM zone + fuse `DEM + max(canopy, building)` into a pseudo-DSM, or pass a true lidar DSM through; tile-keyed, content-addressed, idempotent cache), and `cache_rw` (skip already-built tiles). Added an `Ingestion` config block, a deterministic `tiling.py` pre-step (4.51M unique coords → 5,176 UTM tiles, ~872 coords/tile — the O(tiles) batching unit), a `SurfaceTile` state record, and a standalone `leo-ingestion` CLI. The LLM's only judgement is **which fallback to take on a read failure** (true_dsm → pseudo_dsm → cover_proxy, with lowered confidence); all raster math is deterministic. **Verified live** end-to-end (no API key needed): a real Copernicus GLO-30 DEM window resolved via STAC, signed, read, reprojected to UTM @10 m, and written as a GeoTIFF. **Repurposed the old POC `ingestion` agent** (input load + de-dup): that duty is now a deterministic pre-step (`python -m leo_pipeline.ingest` / `tiling`), not an agent — its `query_locations`/`deduplicate_coordinates` tools stay served and runnable. **Deferred:** building-footprint rasterization (pseudo-DSM currently fuses DEM + canopy) and a full `cover_proxy` (DEM-only, low-confidence for now). Added 40 offline tests + 1 opt-in live COG-read test. |
| 2026-06-03 | **Add a standalone `leo-discovery` CLI + a `tests/` suite, and fix the AOI-override point count** | Wrapped A1 in `python -m leo_pipeline.run_discovery` (`--bbox`/`--out`/`--allow-fallback`/`--dry-run`) so a manifest can be produced for any AOI without the locations CSV, via a process-level `tools.AOI_OVERRIDE`. Added deterministic, offline pytest coverage of the A1 tools + agent contract (one opt-in live test gated on `LEO_RUN_LIVE=1`). **Fixed a bug:** with a `--bbox` override, `get_aoi_bbox` hardcoded `n_total/n_distinct = 0`, so the agent wrote a misleading "CSV had 0 points" note even when the box was full of points; it now counts the CSV points actually inside the override box (new `ingest.count_in_bbox`), e.g. 92,823 points for the central-NC `[-80,35,-78.5,35.1]` box, and still returns 0 only when no CSV is present. |
| 2026-06-03 | **Build the A1 data-discovery agent** — live STAC search + ranked data manifest | Wired the discovery role into `src/`: a `DATA_DISCOVERY_AGENT` plus three live tools — `get_aoi_bbox` (AOI from the locations), `stac_search` (pystac-client over Microsoft Planetary Computer), and `write_data_manifest` (persists the H1-review manifest to `data/interim/`). The agent ranks candidates by resolution/vintage/coverage/licence and is granted `WebSearch`/`WebFetch` to source **tree-canopy height**, which no Planetary Computer collection carries (verified, 134 collections). Confirmed live against the real AOI (Carolinas/N-Georgia): real items returned for 3DEP (10 m), Copernicus GLO-30, NASADEM, and MS Buildings. Filled `docs/data-sources.md` with the curated candidate catalog; added `pystac-client`/`planetary-computer` deps. Keeps the LLM-ranks / code-queries split and writes no rasters (manifest only). |
| 2026-06-03 | **Complete `docs/rationale.md`** — approach justification, plain-language "at-risk" definition, and a consolidated **Tuneable parameters** registry | Justified the per-azimuth horizon-profile approach over a simple buffer, a binary viewshed, and ML obstruction detection; wrote a stakeholder-facing definition of the `clear`/`at-risk`/`undetermined` bands; and consolidated every modeling knob (θ, FOV cone, azimuth weighting, mount-height sweep, σ_H budget, band cut-points, search window, CRS, dedup precision) into one registry with defaults + sensitivities. Added a **per-class, derived** obstacle search radius `R_max = (H_class,max − H_a) / tan θ_min`, and rejected a single global radius as the fixed-buffer anti-pattern. **Considered then rolled back** a per-class RF-opacity weighting (trees attenuate less than opaque buildings/terrain): without canopy-depth data it risks false `clear` verdicts for forest-ringed homes, so all obstacles remain uniform opaque blockers. |
| 2026-06-02 | Document the **obstruction methodology, Starlink reception geometry, and assumptions** in `docs/rationale.md` | Derived the per-obstacle clear-view inequality `H_b − H_a < Dist_ab · tan θ` from the install-guide requirement, and made the two physical inputs — minimum elevation angle θ (default 25°; Apr-2026 FCC relaxation toward 10–20°, 5° above 62°N) and the azimuth/FOV cone (110° Gen3 default, 140° High Performance) — exposed parameters rather than buried constants. Adopted a three-state `clear`/`at-risk`/`undetermined` outcome, a mast-height parameter sweep, parcel-clipped alternative-site suggestions, and cross-source corroboration (nDSM + second footprint source) for "no building found." Drives the `lookup_obstruction_layer` / `compute_risk_score` tool design. |
| 2026-06-02 | De-duplicate to **one work item per unique coordinate** before obstruction sampling (`ingest.deduplicate_coordinates`) | The 4,674,917-row input holds only 4,514,477 unique coordinates — ~160K locations (one shared by 5,496) share a coordinate. Obstruction sampling is a pure function of the coordinate, so sampling per-row repeats expensive raster/vector work for no new information. Writes a unique-coordinate work list + a `location_id→coord_id` fan-out map (Parquet, `data/interim`); coords keyed on integer micro-degrees so the dedup is exact/reproducible. ~3.4% fewer samples at 6 dp, tunable higher by snapping to a coarser grid once raster resolution is known. |
| 2026-06-02 | Treat **duplicate `location_id`s** as a data-quality flag, not a hard failure | Profiling found 12 `location_id`s appearing more than once. The ingestion agent reports them as a quality issue while the dedup map preserves every input row, so the count surfaces for human review without dropping data or blocking the pipeline. |
| 2026-06-02 | Scaffold submission as a dedicated repo under `ready/leo-coverage-risk/` | Keep the graded submission separate from the workspace layer; clean git history from commit #1. |
| 2026-06-02 | Orchestrate with the **Claude Agent SDK** | Native multi-agent (subagents) + in-process MCP tools with schemas; directly satisfies the agent-design deliverable. |
| 2026-06-02 | Reuse the shared `../../.venv`; add only `claude-agent-sdk`, `anthropic`, `python-dotenv` (+ optional `pydeck`) | Geo stack (geopandas/shapely/rasterio/duckdb) already present; avoid a redundant environment. |
| 2026-06-02 | Opus drives orchestration, Sonnet for worker calls | Cost/latency balance under the testing budget. |

_Newest entries on top as work proceeds._
