# AI tools disclosure

Per the challenge requirements, this file discloses every AI tool used, its purpose,
and where its output was accepted as-is versus diverged from / corrected. Kept current
as work proceeds.

## Tools used

| Tool | Model | Purpose |
|------|-------|---------|
| Claude Code (CLI) | Claude Opus 4.8 | Planning, scaffolding the repo, writing code & docs in this session. |
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

> When you accept, reject, or rework AI-generated analysis or code, add a dated entry
> noting what and why. This is graded under "Communication & documentation."
