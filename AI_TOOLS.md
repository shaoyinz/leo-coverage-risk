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

> When you accept, reject, or rework AI-generated analysis or code, add a dated entry
> noting what and why. This is graded under "Communication & documentation."
