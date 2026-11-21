# 13 Public API, chat, and web UI

Code: `src/eye/api/app.py`, `src/eye/api/chat.py`, `web/`. Tasks: T18 (API), T20 (chat), T21 (UI on mocks), T22 (UI live).
P3 builds the UI with an AI coding tool (Devin or Claude Code) against the mock files. No build step, no framework.

## 1. Public API (core, port 8000)

All JSON uses the models in `02-contracts.md`.

| Method + path | Returns | Mock file |
|---|---|---|
| `GET /api/status` | `{sim_ts, tick, running, paused, llm_mode, llm_calls, tokens_in, tokens_out, agents: {name: {ok, url}}, active_scenarios, investigations_running}` | `status.json` |
| `GET /api/graph?node&depth` | `GraphView` | `graph.json` |
| `GET /api/metrics` | list of `MetricDef` (no SQL) | `metrics.json` |
| `GET /api/metrics/{name}?start&end&segment` | `Series` with expected/lower/upper (from `baseline_compare`) | `metric_orders.json` |
| `GET /api/metrics/{name}/definition` | `MetricDef` + in/out edges | `metric_def_orders.json` |
| `POST /api/metrics` | body `AddMetricRequest` → `AddMetricResult` (runs the Cartographer; may take up to 60 s) | `add_metric_result.json` |
| `GET /api/anomalies?since` | recent `Anomaly` + `SentinelDecision` | `anomalies.json` |
| `GET /api/incidents?status` | list of `Incident` (without hypotheses detail) | `incidents.json` |
| `GET /api/incidents/{id}` | `Incident` | `incident_s7.json`, `incident_s6.json` |
| `GET /api/incidents/{id}/trail` | list of `TrailStep` for all its investigations | `trail_s7.json` |
| `GET /api/scenarios` | list of `ScenarioState` | `scenarios.json` |
| `POST /api/scenarios/{id}/inject`, `/clear` | `ScenarioState` | – |
| `POST /api/clock/pause`, `/resume`, `/step` | status | – |
| `GET /api/events` | SSE stream of `Event` | `events.jsonl` (one event per line, replayed by the mock mode) |
| `POST /api/chat` | §2 | `chat_response.json` |
| `GET /api/demo/truth` | `scenario_log` (ground truth) — only when `demo.show_truth: true` in config | `truth.json` |

Static: `GET /` serves `web/index.html`; `/web/*` static files.

## 2. Chat (`api/chat.py`, T20)

- Request: `{messages: [{role: "user"|"assistant", content: str}]}` (max 20 messages, each ≤ 4000 chars).
- The backend opens an MCP client session (`mcp` 2.3.0 streamable HTTP client) to `http://127.0.0.1:8100/mcp`, lists tools, converts each MCP tool to a Claude tool (name, description, input schema), and runs the agent loop (`08-agent-runtime.md`) with a `chat` agent spec. Tool calls go to the MCP session.
- The chat agent's prompt: answer questions about the marketplace and its incidents; always cite incident ids and numbers; use `inject_scenario` only if the user asks for a demo.
- Response: `{answer: str (Markdown), tool_calls: [{tool, input, summary}], incident_ids: [str]}`. Non-streaming; the UI shows a spinner.
- Why through MCP and not direct calls: the chat proves the MCP server works for any agent. Write this in the pitch.

## 3. Web UI (`web/`, T21 + T22)

### 3.1 Layout (one page, desktop 1440 × 900 first)

```
┌ Top bar: "Eye of the System" · sim clock · ▶/⏸ · LLM mode · agents health dots · scenario buttons (S1..S7, I1, I2) ┐
├───────────────┬────────────────────────────────────────┬──────────────────────┤
│ Graph         │ Metric chart (selected node)           │ Chat                 │
│ (Cytoscape)   │ actual line, expected band, markers    │ messages             │
│ node color =  ├────────────────────────────────────────┤ tool calls collapsed │
│ state         │ Incidents list (open / cleared / resolved tabs)               │
│ edge style =  │ Incident detail: title, verdict badge, impact €/h,           │
│ type          │ contributions bar, hypotheses (supported / refuted),         │
│ dimension     │ trail timeline (step, actor icon, tool, summary, reason),    │
│ chips         │ SellerOS notification, suggested actions                     │
├───────────────┴────────────────────────────────────────┴──────────────────────┤
│ Live feed: last 30 events (tick, sentinel decisions, investigation started …)            │
└──────────────────────────────────────────────────────────────────────────────────────────┘
+ "Add metric" modal (name, description, SQL) → shows AddMetricResult and highlights new edges
```

### 3.2 Visual rules

- Node state colors: normal grey, watch amber, anomaly red outline, incident red fill, resolved green outline (300 ms fade).
- Edge styles: formula solid dark line with label "=", causal dashed line with confidence as width, ambiguous dotted amber, rejected hidden (toggle to show).
- Verdict badges: CONFIRMED green, MIXED violet, HYPOTHESIS amber, INNOCENT grey, DATA_ISSUE blue. Text label always present (not color only).
- Contributions: horizontal bar per factor, signed, with the share in %. "planned" rows striped.
- Trail: actor icons (text letters are fine: S, D, K, R, C, G, M, X for SellerOS). External SellerOS steps marked "external".
- Accessibility: all buttons are `<button>`; keyboard focus visible; colors meet contrast 4.5:1; charts have a text summary next to them.

### 3.3 Mock mode

`web/app.js` reads `?mock=1`: then it loads `web/mock/*.json` instead of the API and replays `events.jsonl` every 2 s. P3 builds and reviews the whole UI in mock mode before the backend exists.

### 3.4 Live wiring (T22)

- Replace mock fetches with the API (same paths). SSE `EventSource('/api/events')`; reconnect on error with 2 s backoff.
- `incident_upserted` → refresh list; `trail_step` for the open incident → append to the timeline; `graph_changed` → reload graph; `tick` → clock + chart refresh of the selected node.

## 4. Tests

T18: API contract tests: each endpoint returns JSON valid against its model (pydantic `model_validate`); SSE emits `tick` within 2 × `seconds_per_sim_hour`.
T20: chat with a fake LLM that calls `list_incidents` → the MCP tool is called and its result is in the answer.
T21/T22: manual checklist (P3), in the PR description: each panel shows data; clicking a node opens its chart; clicking an incident opens its trail; add-metric modal works; keyboard navigation reaches all buttons.
