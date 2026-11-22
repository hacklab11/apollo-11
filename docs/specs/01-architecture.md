# 01 Architecture

Read first: `00-overview.md`. Contracts: `02-contracts.md`.

## 1. Processes

| Process | Command | Port | Owns | Talks to |
|---|---|---|---|---|
| `core` | `uv run eye-core` | 8000 | simulator, DuckDB, registry, graph, tools, core agents, incidents, public API, SSE, static UI, data API | Anthropic API, specialists (A2A), SellerOS (A2A) |
| `specialists` | `uv run eye-specialists` | 8200 | 3 A2A agents: Growth, Marketplace, Discovery | core data API (HTTP), Anthropic API |
| `selleros` | `uv run --project packages/selleros selleros` | 8300 | 1 A2A agent + its own data file | Anthropic API only. It never calls core. |
| `mcp` | `uv run eye-mcp` | 8100 | MCP server (streamable HTTP, path `/mcp`) | core public API (HTTP) |

`scripts/dev.sh` starts all four. Deployment with one public URL: `16-deployment.md`. `scripts/reset.sh` deletes `data/` and starts again from the seed.

Why separate processes: A2A and MCP must be real network protocols for the demo. Why specialists use the data API and do not open DuckDB: DuckDB allows one writer process. Core holds the write connection.

## 2. Repository layout

This is a uv workspace; all Eye members contribute to the implicit `eye`
namespace without an `eye/__init__.py`. Every member has its own `pyproject.toml`.

```
apollo-11/
  pyproject.toml                 # non-packaged workspace aggregator, Python >=3.13
  packages/
    agent_runtime/src/agent_runtime/ # reusable harness, no Eye or SellerOS imports
    contracts/src/eye/           # contracts.py, config.py
    data/src/eye/                # schema.py, registry/, graph/, tools/
    llm/src/eye/llm/             # interim runtime, client, prompts/*.md
    agents/src/eye/              # agents/ (five core agents), a2a_client.py
    orchestrator/src/eye/        # orchestrator.py, incidents/, api/,
                                # datasource.py, main.py (eye-core)
    specialists/src/eye/specialists/ # eye-specialists A2A service
    mcp/src/eye/mcp_server.py    # eye-mcp service
    selleros/                    # independent selleros package; no Eye imports
      pyproject.toml
      selleros/                  # server.py, agent.py, data.py, runtime, models
      tests/
      data/                      # generated private seller data (gitignored)
  sim/src/eye/sim/               # simulator, eye-sim CLI, SellerOS export
  config/                       # settings, metrics, identities, causal seeds, scenarios
  web/                          # observatory static assets
  scripts/                      # stack launcher, reset, demo and eval scripts
  tests/                        # Eye tests; SellerOS tests also run from the root
  docs/
  data/                         # generated world, incidents, graph and recordings
```

## 3. Data flow per tick

```
clock tick (every sim.seconds_per_sim_hour real seconds)
  1. sim.step()                       → writes 1 sim-hour of rows, emits SSE "tick"
  2. sentinel.run(ts)                 → decisions (investigate / watch / defer / ignore / attach)
  3. reporter.match(decisions)        → attach to open incident or start new investigation
  4. detective.investigate(anomaly)   → async task; trail steps stream as SSE "trail_step"
        ↳ tools (local), specialists + SellerOS (A2A), skeptic (local agent)
  5. reporter.upsert(result)          → incident created/updated/merged, SSE "incident_upserted"
        ↳ SellerOS notification (A2A) if small sellers are affected
  6. reporter.lifecycle(ts)           → persisting / resolved checks (deterministic)
```

Rules:
- At most `orchestrator.max_concurrent_investigations` investigations run at the same time.
- If `orchestrator.pause_sim_during_investigation` is true, step 1 waits until no investigation runs. This keeps the demo easy to follow.
- The Detective's time window is fixed at the anomaly's `ts`. New data after that does not change the investigation.

## 4. Stack and versions

| Item | Version | Note |
|---|---|---|
| Python | 3.13 via `uv` | system `python3` is 3.9; do not use it |
| `anthropic` | 2.12.0 | Messages API, manual tool loop |
| `a2a-sdk` | 1.3.1 | read its docs before use; the 1.x API differs from 0.x examples |
| `mcp` | 2.3.0 | server (streamable HTTP) and client |
| `duckdb` | 2.0.1 | one write connection in core |
| `fastapi` | 0.165.0 | plus `uvicorn`, `sse-starlette` or native SSE |
| `httpx` | latest | HTTP client |
| `networkx`, `pyyaml`, `pydantic` (v2), `numpy` | latest | |
| `pytest`, `ruff` | latest | dev |
| UI | Cytoscape.js, Chart.js from CDN | no build step |

Versions are the latest on PyPI on 2026-11-21. Pin them in `pyproject.toml`.

### 4.1 Claude API usage (all agents)

- Model: `claude-opus-5-5` for every agent by default (config `llm.models.*`). A cheaper model is a config change and a team decision.
- Thinking: Opus 5.5 always thinks. Do not send `thinking: {type: "disabled"}` or `budget_tokens` (400 error). Control depth with `output_config={"effort": ...}`. Default effort is `medium`. Set it explicitly per agent.
- Tool choice: forced `tool_choice` `any` or `tool` returns 400 on Opus 5.5. Use `auto` and name the tool in the prompt.
- Final answers that code must parse: use structured outputs (`output_config.format` with a JSON schema) or a `submit_*` tool with `strict: true`.
- Parallel tool calls: return all `tool_result` blocks in one user message. Failed tool → `tool_result` with `is_error: true`.
- Always check `stop_reason` before reading content: `tool_use`, `end_turn`, `max_tokens`, `refusal`.
- Refusals: send beta `server-side-fallback-2026-07-01` with `fallbacks: "default"`. If `stop_reason == "refusal"` still occurs, record it in the trail and end the investigation as `HYPOTHESIS`.
- Prompt caching: put `cache_control` on the stable system prompt + tool list. Do not put timestamps or IDs in the system prompt.
- Parse tool inputs as JSON objects. Never compare raw strings.
- Read the official Python SDK docs for exact method names. Do not guess signatures.

Why a manual loop and not Managed Agents or the Tool Runner: the tools run locally against DuckDB, every step must go to the trail and the SSE stream, and the demo needs record/replay of LLM responses (`08-agent-runtime.md`).

## 5. Configuration (`config/settings.yaml`)

Every value has a default. `config.py` validates the range at startup. An invalid value stops startup with a clear error. A missing value uses the default.

| Key | Purpose | Valid range | Default |
|---|---|---|---|
| `sim.seed` | random seed for world and noise | int ≥ 0 | 42 |
| `sim.history_days` | days of history before "now" | 21–60 | 28 |
| `sim.start_ts` | sim "now" at start (UTC) | ISO timestamp | `2026-11-27T00:00:00Z` |
| `sim.seconds_per_sim_hour` | real seconds per sim hour | 0.5–60 | 5 |
| `sim.autostart` | start the clock at boot | bool | true |
| `orchestrator.max_concurrent_investigations` | parallel investigations | 1–4 | 2 |
| `orchestrator.pause_sim_during_investigation` | stop clock while investigating | bool | true |
| `sentinel.top_k` | candidates sent to the Sentinel LLM | 3–30 | 10 |
| `sentinel.z_threshold` | min robust z for a candidate | 2.0–6.0 | 3.0 |
| `sentinel.divergence_pp` | min share shift (percentage points) for a divergent segment | 1.0–20.0 | 3.0 |
| `sentinel.min_completeness` | below this, defer | 0.5–1.0 | 0.9 |
| `detective.max_steps` | step budget per investigation | 5–30 | 15 |
| `detective.confirm_share` | explained share needed for CONFIRMED | 0.5–0.95 | 0.7 |
| `detective.min_independent_evidence` | evidence groups for CONFIRMED | 1–3 | 2 |
| `reporter.resolve_after_ticks` | normal ticks before `resolved` | 1–24 | 3 |
| `reporter.resolve_z` | abs z below which a tick counts as normal | 0.5–3.0 | 2.0 |
| `llm.mode` | `live`, `record`, `replay` | enum | `live` |
| `llm.cache_path` | record/replay file | path | `data/llm_cache.jsonl` |
| `llm.models.default` | model for all agents | model id | `claude-opus-5-5` |
| `llm.models.<agent>` | per-agent override | model id | unset → default |
| `llm.effort.<agent>` | effort per agent | low/medium/high/xhigh/max | sentinel `low`, detective `high`, skeptic `medium`, reporter `medium`, cartographer `high`, specialists `medium`, chat `medium` |
| `llm.max_tokens` | per request | 1024–64000 | 16000 |
| `llm.max_calls_per_hour` | global LLM call cap (real hour); above it no new investigations | 50–5000 | 600 |
| `demo.show_truth` | expose `GET /api/demo/truth` (ground truth) for the demo panel | bool | false |
| `a2a.growth_url`, `a2a.marketplace_url`, `a2a.discovery_url` | Agent Card base URLs | URL | `http://localhost:8200/growth`, `/marketplace`, `/discovery` |
| `a2a.selleros_url` | external agent base URL | URL | `http://localhost:8300` |
| `a2a.timeout_s` | per A2A call | 5–120 | 60 |
| `ports.core`, `ports.mcp`, `ports.specialists`, `ports.selleros` | ports | 1024–65535 | 8000, 8100, 8200, 8300 |

Secrets: `ANTHROPIC_API_KEY` comes from the environment only. Never write it to a file, log, or commit.

## 6. Security and trust boundaries

| Boundary | Input | Check |
|---|---|---|
| `add_metric` (API, MCP, UI) | name, description, SQL | name matches `^[a-z][a-z0-9_]{2,40}$`; SQL passes `sqlguard` (one SELECT statement, no DDL/DML, no `scenario_log`, no `read_csv`/`read_parquet`/`ATTACH`/`COPY`/`INSTALL`/`LOAD`, only allowed tables); run on a read-only cursor with a row limit |
| A2A responses | JSON from other agents | validate against the pydantic model; on failure record "invalid response" in the trail |
| MCP tool inputs | JSON | pydantic validation; enum checks for scenario ids |
| Chat input | free text | length limit 4000 chars; the chat agent has only MCP tools, no shell |
| Scenario ground truth | `scenario_log` table | no agent tool can read it; only `eval_scenarios.py` and the UI demo panel read it |

All servers bind to `127.0.0.1`.

## 7. Observability

- Every LLM call, tool call, and A2A call writes one `TrailStep` (`02-contracts.md`).
- `core` logs to stdout as JSON lines: `ts`, `component`, `event`, `incident_id`, `duration_ms`. No secrets, no full prompts.
- `GET /api/status` shows the clock, tick count, running investigations, agent health (A2A card fetch ok?), LLM call count, and LLM mode.
