# Runbook — running the pipeline end to end

Two ways to run the LEO coverage-risk pipeline:

| Mode | Command | Needs API key? | What it exercises |
|------|---------|----------------|-------------------|
| **Agentic** (real Orchestrator + A1–A5) | `python -m leo_pipeline.orchestrator --run` | **Yes** (`ANTHROPIC_API_KEY`) | LLMs orchestrate + reason; deterministic geo code computes |
| **Deterministic** (no LLM) | `python scripts/run_e2e_offline.py` | No (network only) | The same geo engines, fixed driver instead of agents |

All commands run from the repo root with the shared venv: `../../.venv/bin/python …`.

## Pipeline stages

```
pre-steps   ingest + tiling ............ CSV → unique coords → UTM tiles (data/interim/)
A1 discovery run_discovery .............. ranked data manifest        → H1 human gate
A2 ingestion run_ingestion .............. windowed COG read → fused per-tile DSM (cache)
A3 analysis  run_analysis ............... per-location obstruction % + risk tier
A4 QA        run_qa ..................... input audit + output-anomaly scan → H2 gate
A5 report    run_report ................. county/state rollup + PMTiles map + decision log
```

State flows between stages on disk:
- `data/interim/*.parquet` — dedup work list + tile maps (the join that fans a per-tile
  result back out to every location).
- `data/interim/surfaces/` — A2's content-addressed, idempotent surface cache.
- `data/interim/analysis/findings_<tile>.json` — A3 output, read by A4 and A5.
- `data/interim/qa/qa_report.json` — A4 output, folded into A5's decision log.
- `outputs/` — A5 deliverables. The small reviewer-facing results are committed
  (`decision_log.md`, `aggregates.json`, `coverage_map.png/.html`, `locations.pmtiles`); the
  1GB `locations.geojson` + run logs stay gitignored. Regenerate any time via `leo-report --compute`.

## Deterministic end-to-end (no API key)

A1, A3, A4, A5 each ship a no-API path (`--dry-run` for A1; `--compute` for A3/A4/A5).
**A2 is the only stage without one** — its `run_ingestion` CLI builds the surface only
through the agent. `scripts/run_e2e_offline.py` fills that one gap (resolve a real COG via
`stac_item_read`, fuse it via `fetch_aligned_surface`, walking the same true_dsm → cover_proxy
fallback the A2 agent would), then drives A3→A4→A5 `--compute` over a single tile:

```bash
../../.venv/bin/python scripts/run_e2e_offline.py                 # default demo tile (~321 coords)
../../.venv/bin/python scripts/run_e2e_offline.py --tile 32617_158_806
../../.venv/bin/python scripts/run_e2e_offline.py --no-tiles      # skip tippecanoe PMTiles
```

Network is still required (the surface is a windowed read of a public COG); only the LLM
calls are removed. A cold-cache run of the default tile takes well under a minute.

### Verified run (demo tile `32617_158_806`, central/eastern NC)

- **A2** — a **mosaic** surface: real 3DEP lidar DSM (43% of pixels) composited over a complete
  Copernicus GLO-30 DEM (57% fill), 595×595 @ 10 m, `valid_fraction = 0.97` (was 0.42 for the
  bare lidar), with a per-pixel provenance band.
- **A3** — 321 unique coordinates scored → 321 `clear`, **0 `undetermined`** (was 160). Confidence
  tracks provenance: all 161 lidar points are `high`/`medium`, all 160 DEM-fill points are `low`.
- **A4** — input audit over the real 4.67M-row CSV (0 quarantined); **1** output anomaly
  (`low_confidence_rate` 49.8% — the honest DEM-fill share) → H2 REVIEW. The `undetermined_rate`
  anomaly no longer fires.
- **A5** — county (FIPS 37083) + North Carolina state rollup, a real `locations.pmtiles`
  via tippecanoe, the MapLibre viewer, and an officer `decision_log.md` that folds in the
  A4 anomaly.

> Earlier (pre-mosaic) the same tile gave 160/321 `undetermined` and 78% low-confidence — both
> artifacts of a partial lidar surface with no gap-fill; see the decision-log entry dated 2026-06-04.

## Running individual stages manually

```bash
# pre-steps (idempotent; outputs already in data/interim/)
../../.venv/bin/python -m leo_pipeline.ingest          # CSV → unique_coords.parquet + fan-out map
../../.venv/bin/python -m leo_pipeline.tiling          # 4.51M coords → ~5,176 UTM tiles

# A1 (no-API resolve of the manifest config)
../../.venv/bin/python -m leo_pipeline.run_discovery --bbox -80 35 -78.5 35.1 --dry-run

# A3 / A4 / A5 deterministic paths over a tile already in the surface cache
../../.venv/bin/python -m leo_pipeline.run_analysis --tile <tile_id> --compute
../../.venv/bin/python -m leo_pipeline.run_qa --compute        # whole-run QA → qa_report.json
../../.venv/bin/python -m leo_pipeline.run_report --compute
```

> **A4 filename note:** `run_qa --tile <id> --compute` writes `qa_report_<id>.json`, but A5's
> `_load_anomalies()` only reads `qa_report.json`. Run the **whole-run** form (`run_qa
> --compute`, no `--tile`) so the A5 decision log picks up the anomalies — which is what the
> offline driver does.

## Tests (no API key, offline)

```bash
../../.venv/bin/python -m pytest                                      # 160 deterministic tests
LEO_RUN_LIVE=1 ../../.venv/bin/python -m pytest -m live -k ingestion  # real DEM window read
```


  ---
  Subject: Builders Challenge #50 submission — LEO Satellite Coverage Risk (North Carolina)

  ---
  Hi [reviewer name / Ready team],
  
  Please find my submission for Builders Challenge #50 — an agent-driven geospatial pipeline that flags funded
  locations whose Starlink connectivity is likely degraded by environmental obstructions (tree canopy, terrain, and
  buildings).

  - Repository: https://github.com/shaoyinz/leo-coverage-risk
  - 5-minute walkthrough video: [link]

  What it does. Five Claude-Agent-SDK agents (Opus driver / Sonnet workers) run discovery → surface ingestion →
  per-azimuth horizon analysis → QA → reporting. Code does the deterministic geometry and raster work; the LLM picks
  parameters, triages anomalies, and narrates — each step is auditable and reproducible.

  Headline finding (all 4,514,477 NC locations). 96.4% clear / 3.2% at-risk / 0.4% severe. Household-weighted, ~3.6% 
  (≈169.9K households) are at-risk, concentrated in dense-canopy/built-up counties and the western Blue Ridge —
  priority county Mecklenburg (FIPS 37119, ~26K at-risk households). Read it as a prioritized verify-on-site list, not
  a service guarantee.

  Deliverables (all in the repo, see README "Deliverables map"):
  - Agent system design + architecture diagram, tool schemas, and cross-agent state management
  - Analysis rationale & methodology, data sourcing & quality notes
  - Validation/QA and reporting agents, plus an interactive MapLibre risk map (bonus) and an officer-facing decision
  log
  - AI-tool disclosure (AI_TOOLS.md)
  
  One scope note, for transparency. The full 4.5M-location run was executed with the deterministic engine — the same 
  c]ode the agents call through their tools — rather than driving the LLM over all 5,176 tiles, which would have been
  cost-prohibitive on the testing budget. The agentic orchestration (all five agents, LLM in the loop) is verified
  end-to-end on a representative tile, so the agent layer is demonstrably live and every reported number is
  reproducible.

  Happy to walk through any part in more detail. Thank you for the opportunity.