# LEO Satellite Coverage Risk Analysis

An **agent-driven geospatial data pipeline** that identifies locations inside a LEO
satellite provider's service footprint whose connectivity is likely degraded by
environmental obstructions — tree canopy, terrain, and structures.

Submission for the Ready Builders Challenge
([issue #50](https://github.com/ready/builders-challenge/issues/50)).

## Status

Repository scaffold. Pipeline logic to follow. See the [decision log](#decision-log).

## Layout

```
docs/        architecture, rationale, data-sources (+ Mermaid diagram)
src/leo_pipeline/
  config.py        models, paths, env loading
  orchestrator.py  wires tools + agents into Claude Agent SDK options
  agents/          ingestion · geo-analysis · qa (least-privilege tool access)
  tools/           @tool defs + in-process SDK MCP server ("leo")
  state/           PipelineState threaded between agents
notebooks/   00_data_inspection.ipynb — first-pass CSV profiling
data/raw/    provided locations.csv (gitignored; supplied by the challenge)
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

## Step-0 pre-work (from the Install Guide)

> To complete after reading the Starlink Install Guide — see `docs/rationale.md`.

1. **Physical conditions causing service interruptions** — _TODO_
2. **Environmental requirements for dish connectivity** — _TODO_
3. **Public geospatial datasets to model these at scale** — _TODO_ (see `docs/data-sources.md`)
4. **Limitations of remote analysis** — _TODO_

## Deliverables map

| Deliverable | Where |
|-------------|-------|
| Agent system design & architecture diagram | `docs/architecture.md`, `docs/diagrams/architecture.mmd` |
| Tool definitions with schemas | `src/leo_pipeline/tools/` |
| State management between agents | `src/leo_pipeline/state/` |
| Analysis rationale & methodology | `docs/rationale.md` |
| Data sourcing & quality | `docs/data-sources.md`, `notebooks/00_data_inspection.ipynb` |
| AI-tool disclosure | `AI_TOOLS.md` |

## Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-06-02 | De-duplicate to **one work item per unique coordinate** before obstruction sampling (`ingest.deduplicate_coordinates`) | The 4,674,917-row input holds only 4,514,477 unique coordinates — ~160K locations (one shared by 5,496) share a coordinate. Obstruction sampling is a pure function of the coordinate, so sampling per-row repeats expensive raster/vector work for no new information. Writes a unique-coordinate work list + a `location_id→coord_id` fan-out map (Parquet, `data/interim`); coords keyed on integer micro-degrees so the dedup is exact/reproducible. ~3.4% fewer samples at 6 dp, tunable higher by snapping to a coarser grid once raster resolution is known. |
| 2026-06-02 | Treat **duplicate `location_id`s** as a data-quality flag, not a hard failure | Profiling found 12 `location_id`s appearing more than once. The ingestion agent reports them as a quality issue while the dedup map preserves every input row, so the count surfaces for human review without dropping data or blocking the pipeline. |
| 2026-06-02 | Scaffold submission as a dedicated repo under `ready/leo-coverage-risk/` | Keep the graded submission separate from the workspace layer; clean git history from commit #1. |
| 2026-06-02 | Orchestrate with the **Claude Agent SDK** | Native multi-agent (subagents) + in-process MCP tools with schemas; directly satisfies the agent-design deliverable. |
| 2026-06-02 | Reuse the shared `../../.venv`; add only `claude-agent-sdk`, `anthropic`, `python-dotenv` (+ optional `pydeck`) | Geo stack (geopandas/shapely/rasterio/duckdb) already present; avoid a redundant environment. |
| 2026-06-02 | Opus drives orchestration, Sonnet for worker calls | Cost/latency balance under the testing budget. |

_Newest entries on top as work proceeds._
