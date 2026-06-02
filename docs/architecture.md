# Architecture — agent system design

> Status: scaffold. Sections below mirror the challenge's "Data Ingestion & Analysis
> Workflow" deliverable. Fill in as the pipeline is built.

## Diagram

See [`diagrams/architecture.mmd`](diagrams/architecture.mmd) (Mermaid). Shows agents,
tools, communication flow, data, and human intervention points.

## Agents, boundaries & scopes

Defined in [`src/leo_pipeline/agents/__init__.py`](../src/leo_pipeline/agents/__init__.py).
Tool access is least-privilege per agent.

| Agent | Scope | Tool access |
|-------|-------|-------------|
| `ingestion` | Load & profile the locations dataset; flag data-quality issues | `query_locations` (read-only) |
| `geo-analysis` | Sample obstruction layers per clean location; compute risk scores | `query_locations`, `lookup_obstruction_layer`, `compute_risk_score` |
| `qa` | Validate analysis outputs for anomalies before reporting | `query_locations` (read-only) |

## Tool definitions & schemas

Defined as an in-process SDK MCP server (`leo`) in
[`src/leo_pipeline/tools/__init__.py`](../src/leo_pipeline/tools/__init__.py):

- `query_locations(sql: str, limit: int)` — read-only DuckDB query over the dataset.
- `lookup_obstruction_layer(lat: float, lon: float, layer: str)` — sample canopy / terrain / structure layer.
- `compute_risk_score(factors: dict)` — combine factors into a 0..1 score + band.

## State management

`PipelineState` in [`src/leo_pipeline/state/__init__.py`](../src/leo_pipeline/state/__init__.py)
is threaded between agents: stage, record counts, quality issues, risk findings, notes,
and an error slot. The orchestrator checkpoints it so a human can inspect/intervene
between stages.

## Failure handling & intervention points

- _TODO:_ bad/missing data → ingestion agent records `DataQualityIssue`, orchestrator
  decides continue vs. halt.
- _TODO:_ anomalous analysis results → `qa` agent returns the run for rework.
- _TODO:_ tool errors / rate limits → retry policy + surfacing to the human reviewer.
