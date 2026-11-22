# 12 MCP server

Code: `packages/mcp/src/eye/mcp_server.py`. Task: T19.
Package: `mcp` 2.3.0. Read the official Python SDK docs for the 2.x server API (decorators, streamable HTTP app). Do not copy 1.x examples without checking.

## 1. Design

- Transport: streamable HTTP at `http://127.0.0.1:8100/mcp`. Public through Caddy at `/mcp` (`16-deployment.md`).
- The MCP server is a thin adapter over the core public API (`13-api-and-ui.md`) using `httpx`. It holds no state and no DB connection. Why: one source of truth; the MCP server can restart any time.
- Server name `eye-of-the-system`, instructions text: "Monitoring system for a simulated marketplace. Start with system_status and list_incidents. Incidents contain evidence trails."

## 2. Tools

| Tool | Input | Output | Core API |
|---|---|---|---|
| `system_status` | – | clock, tick, agents health, LLM mode | `GET /api/status` |
| `list_incidents` | `status?` (enum), `limit?` ≤ 50 | id, title, status, verdict, impact, opened/updated | `GET /api/incidents` |
| `get_incident` | `id` | full `Incident` + trail steps (summaries only, max 60) | `GET /api/incidents/{id}` + `/trail` |
| `get_graph` | `node?`, `depth?` ≤ 3 | `GraphView` (trimmed to 80 nodes) | `GET /api/graph` |
| `query_metric` | `name`, `start?`, `end?` (default last 48 h), `segment?` | series with expected band | `GET /api/metrics/{name}` |
| `explain_metric` | `name` | definition, SQL, dimensions, in/out edges with types and status | `GET /api/metrics/{name}/definition` |
| `add_metric` | `name`, `description`, `sql` or `expression` | `AddMetricResult` | `POST /api/metrics` |

Every tool description says what it returns and when to use it. Output is JSON text plus a one-line human summary first.

Scenario controls belong exclusively to the [simulator service](../../sim/README.md); MCP cannot trigger, clear or list them.

## 3. Resources (optional, cut if late)

- `eye://incidents/{id}` → incident report as Markdown.
- `eye://graph` → graph as Mermaid text.

## 4. Errors

Core API error → MCP tool error result with the HTTP status and message. Core down → error "core unavailable".

## 5. Tests (T19)

1. Start core (test mode, no LLM) + MCP; an `mcp` client session lists 7 tools.
2. `list_incidents` returns valid JSON against a seeded incident.
3. Scenario tools are not exposed and tool listing makes no core requests.
4. Manual check (write the result in the PR): Claude Code connects with `claude mcp add --transport http eye http://localhost:8100/mcp` and answers "what incidents are open?".
