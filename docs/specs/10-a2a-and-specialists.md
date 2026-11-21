# 10 A2A layer and the three specialists

Code: `packages/orchestrator/src/eye/a2a_client.py`, `packages/specialists/src/eye/specialists/`, `packages/orchestrator/src/eye/api/data_api.py`. Tasks: T15 (data API + A2A client), T16 (specialists).
Package: `a2a-sdk` 1.3.1. Read its official docs and README first. The 1.x API differs from the 0.x examples on the internet. Do not guess class names. If a name in this spec does not exist in 1.3.1, use the 1.3.1 equivalent and note it in the PR.

## 1. Protocol use

- Each agent publishes an Agent Card at the well-known path the SDK serves (in recent A2A versions `/.well-known/agent-card.json`; check 1.3.1).
- Transport: JSON-RPC over HTTP, the SDK default. Streaming is not needed.
- Message format: the request message has one data part with `SpecialistRequest` JSON and one text part with `question`. The response is a completed task with one artifact; the artifact has one data part with `SpecialistResponse` JSON.
- Task states used: `completed`, `failed`. `failed` carries an error text.

## 2. Agent Cards

| Agent | Base URL | Skills (id: description) |
|---|---|---|
| Growth | `http://localhost:8200/growth` | `ads_pacing`: is an advertiser's spend on schedule; `auction_health`: CPC, bidders, slot fill; `traffic_sources`: marketing spend and attributed sessions; `discounts`: platform-funded discounts |
| Marketplace | `http://localhost:8200/marketplace` | `buybox`: who holds the Buy Box and why; `pricing`: price moves and repricer-like patterns; `availability`: stock, stockouts, capacity; `purchasing`: 1P POs and deliveries; `new_sellers`: recently joined sellers and their offers |
| Discovery & Experience | `http://localhost:8200/discovery` | `search_quality`: relevance, CTR, conversion by country/category; `feed_exposure`: what the For You feed shows; `checkout_funnel`: carts → checkout → payment by platform; `client_errors`: errors by app version and page; `components`: which platform uses which front-end component |
| SellerOS (external) | `http://localhost:8300` | see `11-selleros-external.md` |

Card fields: name, description, version `0.1.0`, url, skills, default input/output mode `application/json` and `text/plain`.

## 3. A2A client in core (`a2a_client.py`)

```python
class A2AClient:
    async def fetch_cards(self) -> dict[str, AgentCardInfo]          # at startup + every 60 s; health for /api/status
    async def ask(self, agent: str, req: SpecialistRequest) -> SpecialistResponse
    async def notify_seller_os(self, alert: SellerAlert) -> SellerAlertAck
```
- Timeout `a2a.timeout_s`. On timeout, connection error, or invalid JSON: return `SpecialistResponse(agent, answered=False, findings=[])` and the Detective's trail gets "agent unavailable: <reason>". Never raise into the Detective.
- Validate every response with pydantic.

## 4. Specialist service (`specialists/app.py`)

- One process, one ASGI app, three A2A agent apps mounted at `/growth`, `/marketplace`, `/discovery`.
- Each specialist = `run_agent` (`08-agent-runtime.md`) with:
  - its system prompt (`prompts/growth.md`, `prompts/marketplace.md`, `prompts/discovery.md`; domain notes from P3),
  - its domain tools = HTTP calls to the core data API (`07-tools.md` §4) plus `metric` and `changes`,
  - `submit_model = SpecialistResponse`,
  - effort `medium`, max steps 8.
- The specialist answers only the question asked, with numbers. It lists `supports` / `refutes` cause types per finding. It never claims a root cause for the whole incident.
- Trail: specialists return their own step list in the response artifact metadata (`steps: [{tool, summary}]`). Core adds them to the trail as `TrailStep(actor=<specialist>, action="tool_call")` under the Detective's `a2a_call` step. This makes their work visible in the UI.

## 5. Data API (`api/data_api.py`)

- Mounted in core at `/data`. Read-only. Uses DuckDB cursors.
- Bound to localhost; not exposed through Caddy (`16-deployment.md`).
- Every endpoint: validate query params (enums, ISO timestamps, `start < end`, window ≤ 14 days). Row limit 5 000.
- Responses: `{rows: [...], summary: "..."}`.

## 6. Tests

T15:
1. Data API: each endpoint returns valid JSON for a baseline window; bad params → 422.
2. A2A client against a stub A2A server (built with the SDK): request/response round-trip; timeout → `answered=False`.
T16:
1. Each specialist card is served and lists its skills.
2. Replay fixture: Discovery answers the S7 question with a finding that names `checkout-web v5.8` and the `TypeError` count.
3. Replay fixture: Growth answers S1 with "ADV-3 spent 100% of budget by 10:00 vs 38% planned".
4. A specialist with the core data API down → task `failed` with a clear message (no hang).
