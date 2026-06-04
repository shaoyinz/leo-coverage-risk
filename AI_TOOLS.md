# AI tools disclosure

Per the challenge requirements, this file discloses every AI tool used, its purpose,
and where its output was accepted as-is versus diverged from / corrected. Kept current
as work proceeds.

## Tools used

| Tool | Model | Purpose |
|------|-------|---------|
| Claude Code (CLI) | Claude Opus 4.8 | Planning, scaffolding the repo, writing code & docs in this session. |
| Web search / fetch (via Claude Code) | — | (1) Gathering and citing public sources for the Starlink reception parameters (minimum elevation angle, dish field-of-view, the 2026 FCC elevation change) in `docs/rationale.md`. (2) Granted to the runtime **data-discovery agent** as the built-in `WebSearch`/`WebFetch` tools, to source off-catalog datasets (tree-canopy height) and licensing terms not carried by any STAC catalog. |
| Claude Agent SDK (`claude-agent-sdk`) | Opus 4.8 (driver) / Sonnet 4.6 (workers) | Runtime multi-agent orchestration of the pipeline itself (ingestion / data-discovery / geo-analysis / qa) and custom tool execution. |
| Anthropic SDK (`anthropic`) | — | Reserved for direct Messages API calls and token/usage metrics (agent-monitoring bonus). |

## Log

### 2026-06-02 — Repo initialization
- **Used:** Claude Code (Opus 4.8) to design and generate the repository scaffold:
  directory structure, `pyproject.toml`, source skeleton (config/agents/tools/state/
  orchestrator), docs placeholders, this file, and the README decision log.
- **Accepted as-is:** directory layout, dependency selection, tool/agent boundary design.
- **Diverged / corrected:** N/A
- **Verified:** SDK API surface checked against the installed `claude-agent-sdk 0.2.87`
  (AgentDefinition fields, ClaudeAgentOptions params, `@tool`/`create_sdk_mcp_server`
  signatures); `python -m leo_pipeline.orchestrator` runs and prints the wired config.

### 2026-06-02 — Analysis methodology & Starlink reception parameters
- **Used:** Claude Code (Opus 4.8), with web search, to derive the obstruction geometry
  (the per-obstacle clear-view inequality `H_b − H_a < Dist_ab · tan θ`) and to research
  and document the Starlink reception parameters — minimum elevation angle, dish
  field-of-view/cone, and the Apr-2026 FCC elevation change — together with the
  assumptions and parameter defaults in `docs/rationale.md`.
- **Accepted as-is:** the geometric derivation; the decision to expose θ and the FOV cone
  as parameters with conservative defaults (θ = 25°, 110° Gen3 cone); the three-state
  `clear` / `at-risk` / `undetermined` outcome.
- **Diverged / corrected:** Manual input the mathematical formula because implementation from AI lean towards the easiest way.
- **Verified:** every external numeric claim (25°/10–20° elevation, 110°/140° cone,
  1–5% / ~10% risk bands) is cited inline in the rationale with its source; the 25°
  distance factor was sanity-checked against 1/tan 25° ≈ 2.1.

### 2026-06-03 — Rationale completion: approach justification, at-risk definition, tuneable parameters
- **Used:** Claude Code (Opus 4.8) to finish `docs/rationale.md` — the "Why this approach"
  comparison (simple buffer / binary viewshed / ML detection vs. the per-azimuth horizon
  profile), the non-technical "Definition of at-risk", and a consolidated **Tuneable
  parameters** registry — plus a per-class, derived obstacle search-radius parameter.
- **Accepted as-is:** the approach justification and the tuneable-parameters registry; the
  per-class **derived** search radius `R_max = (H_class,max − H_a) / tan θ_min`, with a
  single global radius rejected as the fixed-buffer anti-pattern.
- **Diverged / corrected:** AI proposed a per-class **RF-opacity weighting** (trees attenuate
  less than opaque buildings/terrain, via Beer–Lambert / ITU-R P.833) — **rolled back at the
  reviewer's direction**: without canopy-depth data a tree discount risks false `clear`
  verdicts for the forest-ringed homes this tool exists to flag, so the model keeps all
  obstacles as uniform opaque blockers.
- **Verified:** distance-window figures recomputed (1/tan 25° ≈ 2.1, 1/tan 10° ≈ 5.7;
  20 m tree ≈ 55 m, 300 m ridge ≈ 1.7 km); internal anchor links checked; grep confirmed no
  stale opacity / ITU references remain after the rollback.

### 2026-06-03 — Data-discovery agent (A1): live STAC search + ranked data manifest
- **Used:** Claude Code (Opus 4.8) in plan mode to design and implement the A1
  data-discovery agent — three live tools (`get_aoi_bbox`, `stac_search`,
  `write_data_manifest`), the `DATA_DISCOVERY_AGENT` definition (granted `WebSearch`/
  `WebFetch`), the orchestrator wiring, and the `docs/data-sources.md` candidate catalog.
  Added `pystac-client` + `planetary-computer` dependencies.
- **Accepted as-is:** live search over Microsoft Planetary Computer; `stac_search` as a
  per-collection **coverage/metadata probe** (not a tile download); the persisted JSON
  manifest as the H1 review artifact.
- **Diverged / corrected:** scope was **reviewer-directed via clarifying questions** —
  chose *live* STAC search (not schema stubs), *granting web tools* for off-catalog canopy
  height, and *pystac-client* over a hand-rolled `requests` client. The agent derives its
  AOI through a dedicated deterministic `get_aoi_bbox` tool rather than being given raw SQL
  access, keeping its tool surface least-privilege and honouring the "code computes, LLM
  ranks" boundary.
- **Verified:** live smoke tests — `stac_search` returns real items at 100% AOI coverage
  for `3dep-seamless` (10 m), `cop-dem-glo-30` (30 m), `nasadem`, and `ms-buildings`;
  tree-canopy height confirmed **absent** from Planetary Computer (134 collections),
  validating the web-research path; `write_data_manifest` persists
  `data/interim/data_manifest.json` (gitignored); `python -m leo_pipeline.orchestrator`
  prints the agent + tools wired.

### 2026-06-03 — Standalone discovery CLI, test suite, and AOI-override bug fix
- **Used:** Claude Code (Opus 4.8) to add a standalone `python -m leo_pipeline.run_discovery`
  CLI (`--bbox`/`--out`/`--allow-fallback`/`--dry-run`) wrapping the A1 agent via a
  process-level `tools.AOI_OVERRIDE`, and a deterministic `tests/` pytest suite (A1 tools +
  agent contract, offline; one opt-in live test gated on `LEO_RUN_LIVE=1`).
- **Accepted as-is:** the CLI argument surface and the offline/mocked test design (STAC and
  DuckDB stubbed) so the suite runs fast with no API key.
- **Diverged / corrected:** found and fixed a **real bug** surfaced while reviewing a
  `--bbox` run — `get_aoi_bbox` hardcoded `n_total/n_distinct = 0` for any override AOI, so
  the agent wrote a misleading "locations CSV had 0 points" note even when the box was full.
  Added `ingest.count_in_bbox` and wired it in so an override now counts the CSV points
  actually inside the box (and still returns 0 only when no CSV is present); replaced the
  override unit test with hermetic cases asserting both the in-box count and the no-CSV path.
- **Verified:** full suite passes (35 deterministic tests); `count_in_bbox` checked directly
  against the real 4.67M-row CSV — 92,823 points (91,936 distinct coords) inside the
  central-NC `[-80,35,-78.5,35.1]` box, 0 for an out-of-data box; the `get_aoi_bbox` tool
  returns those same counts with `source="cli_override"`.

### 2026-06-03 — Surface-ingestion agent (A2): live windowed COG read + fused pseudo-DSM
- **Used:** Claude Code (Opus 4.8) to design and build the A2 role end-to-end: an `Ingestion`
  config block, a deterministic `tiling.py` pre-step (UTM grid over the de-duplicated work
  list), three least-privilege tools (`stac_item_read`, `fetch_aligned_surface`, `cache_rw`)
  doing live windowed `rasterio` reads + reproject + `DEM + max(canopy, building)` fusion into
  a tile-keyed, content-addressed surface cache, a new `ingestion` agent + `SurfaceTile` state,
  a standalone `leo-ingestion` CLI, and an offline + opt-in-live test suite.
- **Accepted as-is:** the deterministic-code / LLM-only-picks-fallback split (the LLM never
  does raster math, only chooses which surface_mode to retry on a read failure); the
  content-addressed cache keyed on inputs with SAS tokens stripped, giving idempotent resume;
  reusing the A1 `_summarize_stac_item` / pystac-client pattern for `stac_item_read`.
- **Diverged / corrected:** kept the geo stack **pure rasterio** (no rioxarray/odc-stac, which
  are absent from the shared venv) — windowed read via `src.window` + `rasterio.warp.reproject`
  onto a grid derived deterministically from `(bbox, crs, gsd)` so two layers fuse pixel-aligned.
  Consciously **scoped down to a "live core"**: building-footprint rasterization and a full
  cover_proxy are documented deferred gaps (pseudo-DSM fuses DEM + canopy for now), surfaced in
  the agent prompt, docstrings, README, and architecture doc rather than silently faked.
- **Verified:** 75 offline tests pass (40 new: tiling, fuse/cache helpers, the tool dispatch +
  idempotency + fallback paths, agent contract, CLI plumbing). The **live path was confirmed
  without an API key** — a real Copernicus GLO-30 DEM window over a tiny Carolinas AOI was
  resolved via STAC, signed, read, reprojected to UTM @10 m, and written as a GeoTIFF
  (`LEO_RUN_LIVE=1 pytest -m live -k ingestion`). Tiling the real work list collapses 4.51M
  unique coordinates to 5,176 UTM tiles (~872 coords/tile) — the O(tiles) batching unit.

### 2026-06-03 — Analysis agent (A3): live per-azimuth horizon profile + obstruction scoring
- **Used:** Claude Code (Opus 4.8) to design and build the A3 role end-to-end: a deterministic
  `horizon.py` engine (bilinear `SurfaceSampler`, ray-marched per-azimuth horizon profile,
  dwell-time-weighted blocked-sky fraction, three-state tiering + confidence), the two
  architecture A3 tools (`compute_sky_obstruction`, `find_clear_sky_spot`), a version-stamped
  `Analysis` obstruction spec, an `ObstructionResult` state record, a rewritten `geo-analysis`
  agent, a standalone `leo-analysis` CLI (with a no-API `--compute` path), and an offline test
  suite over real synthetic GeoTIFF surfaces.
- **Accepted as-is:** the LLM-picks-params / code-computes split (the agent only chooses the
  mount-height sweep and edge handling; all horizon math is deterministic numpy); the
  per-azimuth horizon profile over a radial buffer (per `docs/rationale.md`); the directional
  cone + north-biased dwell weighting with a southern GSO keep-out de-weight.
- **Diverged / corrected:** **replaced** the placeholder `lookup_obstruction_layer` /
  `compute_risk_score` stubs with the architecture's real A3 tools rather than filling the
  stubs, since their schemas did not match the documented A3 contract. Tightened the σ_H
  "near-cut" confidence band so an unambiguous 0%-obstruction location is not penalised.
  Consciously scoped to a "live core": **σ_H is folded into the confidence flag** rather than a
  full probabilistic clearance-margin model, and `az_weighting='tle_derived'` falls back to the
  static north-biased gradient — both documented as deferred in the prompt, docstrings, README,
  and architecture doc rather than silently faked.
- **Verified:** 25 new offline tests (real-GeoTIFF surfaces: flat → clear, northern wall →
  severe while the same wall to the south stays clear under the directional cone, masting the
  dish lowers obstruction, off-surface → undetermined; single-obstacle horizon angle ≈
  `arctan(Δh/d)`; agent contract + CLI plumbing). Full suite 100 passed / 2 skipped (opt-in
  live). The `leo-analysis --compute` path runs the same engine offline end-to-end. **Not yet
  agentically verified** — no live A3 agent run has been driven; only the deterministic engine
  and tool wiring are exercised.

### 2026-06-03 — Validation/QA agent (A4): live input-quality audit + output-anomaly scan
- **Used:** Claude Code (Opus 4.8) to design and build the A4 role end-to-end: a deterministic
  `qa.py` engine (input-quality DuckDB audit; output-anomaly rules — saturated region,
  tier↔pct consistency, coverage budgets, degenerate distribution, spec drift), a
  version-stamped `config.QA` spec, two least-privilege tools (`qa_input_audit`,
  `qa_location_batch`), a `QAAnomaly` state record, a rewritten `qa` agent, a standalone
  `leo-qa` CLI (with a no-API `--compute` path), and an offline test suite.
- **Accepted as-is:** the rules-detect / LLM-only-triages split (the agent never computes
  stats or re-scores — it explains a flagged anomaly and routes it to the H2 gate); reusing
  `config.Analysis` band cut-points for the tier-consistency check rather than duplicating
  thresholds; the `DataQualityIssue` (input) + `QAAnomaly` (output) state split.
- **Diverged / corrected:** scope set by **clarifying questions** to the user — chose **both**
  input quality + output anomalies (over output-only) and **both** tile + county grouping (over
  tile-only). Caught two issues in self-review before they shipped: (1) the degenerate-
  distribution rule was gated behind `min_region_size` and so missed mid-size groups — split it
  onto its own `degenerate_group_min` gate; (2) it flagged a legitimately **all-clear** region
  (all `pct=0`) as "degenerate" — restricted the rule to a repeated **non-zero** constant, since
  an all-zero region is the common benign case. Also corrected the county grouping from the raw
  15-digit `geoid_cb` **census block** (groups of ~1, never flagged) to the **5-digit county
  FIPS** the architecture's "county at 100% at-risk" example actually means.
- **Verified:** 35 new offline tests (engine rules, both tools, CLI plumbing; in-memory DuckDB
  for the input audit). Full suite 135 passed / 2 skipped (opt-in live). End-to-end offline:
  `qa_input_audit` ran over the real 4.67M-row CSV (0 quarantined — clean, in-AOI), and a
  saturated synthetic tile flagged **both** tile- and county-level (Mecklenburg, FIPS 37119)
  anomalies via the live dedup-map join. **Not yet agentically verified** — no live A4 agent run
  has been driven (same API-key budget block as A2/A3); the engine + tool wiring are exercised.

### 2026-06-03 — Reporting/insight agent (A5): county/state aggregates + interactive map + decision log
- **Used:** Claude Code (Opus 4.8) to design and build the A5 role end-to-end: a deterministic
  `report.py` engine (county+state household-weighted aggregation; a GeoJSON→PMTiles→MapLibre
  interactive map; a plain-English decision-log renderer), a version-stamped `config.Reporting`
  spec, three least-privilege tools (`aggregate_findings`, `render_map`, `write_report`), a
  `ReportArtifact` state record, the `reporting` AgentDefinition, a standalone `leo-report` CLI
  (with a no-API `--compute` path), and an offline test suite. This completes the
  Orchestrator + A1–A5 design in `src/`.
- **Accepted as-is:** the code-aggregates / LLM-writes-narrative split (the agent never invents
  a number — it cites `aggregate_findings` output and composes prose); reusing the A4 county-FIPS
  + `n_locations` dedup-map join for household weighting; reusing `qa`'s tier constants rather
  than redefining them.
- **Diverged / corrected:** scope set by **clarifying questions** to the user — chose
  **tippecanoe→PMTiles+MapLibre** for the map (over pydeck, which isn't installed, and over a
  static matplotlib choropleth) and **county + state rollup** as the reporting unit. Hardened
  two things over the naive AI version: made `render_map` **degrade gracefully** when tippecanoe
  is absent (GeoJSON + HTML still written, the note says so) instead of failing, and gave
  `write_report` a **path-sanitising guard** (basename-only, extension allow-list) so the LLM's
  one write surface can't traverse or drop executable files. Kept the officer report
  **caveat-first** (undetermined/low-confidence share, "verify on site, not a guarantee", the
  spec_version, the A4 H2 status) per `docs/rationale.md`, rather than leading with confident
  numbers.
- **Verified:** 25 new offline tests (aggregation, GeoJSON, the MapLibre HTML builder, the
  decision-log template, the tools, and the CLI; the real-tippecanoe PMTiles test is `skipif`-
  gated on the binary). Full suite 160 passed / 2 skipped (opt-in live). End-to-end offline: a
  synthetic 60-finding run produced the county+state rollup, a real `locations.pmtiles` via
  tippecanoe, the MapLibre `coverage_map.html`, and the `decision_log.md`. **Not yet
  agentically verified** — no live A5 agent run (same API-key budget block as A2–A4); the engine
  + tool wiring are exercised.

> When you accept, reject, or rework AI-generated analysis or code, add a dated entry
> noting what and why. This is graded under "Communication & documentation."
