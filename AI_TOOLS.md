# AI tools disclosure

Per the challenge requirements, this file discloses every AI tool used, its purpose,
and where its output was accepted as-is versus diverged from / corrected. Kept current
as work proceeds.

## Tools used

| Tool | Model | Purpose |
|------|-------|---------|
| Claude Code (CLI) | Claude Opus 4.8 | Planning, scaffolding the repo, writing code & docs in this session. |
| Web search / fetch (via Claude Code) | — | Gathering and citing public sources for the Starlink reception parameters (minimum elevation angle, dish field-of-view, the 2026 FCC elevation change) documented in `docs/rationale.md`. |
| Claude Agent SDK (`claude-agent-sdk`) | Opus 4.8 (driver) / Sonnet 4.6 (workers) | Runtime multi-agent orchestration of the pipeline itself (ingestion / geo-analysis / qa) and custom tool execution. |
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

> When you accept, reject, or rework AI-generated analysis or code, add a dated entry
> noting what and why. This is graded under "Communication & documentation."
