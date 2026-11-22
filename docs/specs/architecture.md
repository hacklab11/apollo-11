# Architecture

Eye is a monitoring and incident workspace: deterministic tools measure, agents interpret,
and humans approve seller actions and close recovered incidents.

## Explore the system

[Interactive service map and animated S8 walkthrough](../architecture/walkthrough.html)
is a single offline HTML file. GitHub shows its source: clone the repo and run
`open docs/architecture/walkthrough.html` (macOS), or open the file in your browser.
Click services for their contracts; play, pause or step through an illustrative S8 run.
Delegation is model-selected, not a guaranteed fixed itinerary.

[![Interactive architecture service map during an S8 challenge](../architecture/walkthrough.png)](../architecture/walkthrough.html)

Explore the service map and follow an illustrative S8 incident offline.

## Compose deployment and protocols

Network membership is shown in labels because services bridge networks. `agents` and
`warehouse` are internal networks; `eye` is not. Arrows show logical communication,
not firewall rules. HTTP is internal plaintext in local Compose. A2A calls use scoped
OAuth JWTs and verified signed cards; the local nginx operator identity is separate.

```mermaid
flowchart TB
  user[Human browser] -->|HTTP localhost:8080| web["web · nginx + React · eye"]
  client[MCP client] -->|MCP streamable HTTP| mcp["eye-mcp · eye"]
  web -->|HTTP /api + SSE| core["eye-core · FastAPI + DBOS · eye / agents / warehouse"]
  mcp -->|HTTP| core
  core -->|PostgreSQL| pg["postgres · eye · DBOS + control"]
  core -->|DuckDB| incidents[(Incident and trail store)]
  core -->|A2A HTTP detect| sentinel["sentinel · agents / warehouse"]
  core -->|A2A HTTP investigate| detective["detective · agents / warehouse"]
  core -->|A2A HTTP report| reporter["reporter · agents"]
  core -->|A2A HTTP map| cart["cartographer · agents / warehouse"]
  detective -->|A2A HTTP challenge| skeptic["skeptic · agents / warehouse"]
  detective -->|A2A HTTP delegation| specialists["eye-specialists · Growth / Marketplace / Discovery · agents / warehouse"]
  detective -->|A2A HTTP seller question| seller["selleros · eye / agents"]
  core -->|A2A alerts + HTTP approved actions| seller
  sim["sim · eye · private world DuckDB"] -->|publish files, manifest last| lake[(sim-lake · public Parquet)]
  lake -->|ParquetLanding / DuckDB| data["eye-data :8150 · warehouse · metrics / graph / tools"]
  lake -->|read-only DuckDB snapshot| cart
  core & sentinel & detective & skeptic & specialists & cart -->|HTTP warehouse operations| data
  sim <-->|shared seller-data volume| seller
  callers["Core / agents / specialists / SellerOS / sim"] -->|HTTP token / JWKS / card signing| auth["auth :8500 · eye / agents"]
  agents["Core agents + specialists"] -->|HTTP proxy / CONNECT :3128| proxy["egress-proxy · agents / eye"]
  proxy -->|allowlisted HTTPS| models[External model providers]
  seller -->|HTTPS model API, not proxy-configured| models
  telemetry[Instrumented Python services] -->|OTLP HTTP :4318| lgtm["lgtm · eye / agents / warehouse · Grafana / Loki / Tempo / Prometheus"]
  user -->|HTTP localhost:3000| lgtm
```

All Compose services are included above. `sim-lake`, incident storage, caller and telemetry
groups are explanatory nodes, not extra services. Host loopback also exposes sim (8100),
SellerOS (8300–8309) and MCP (8110–8119); agents and warehouse have no host ports.
Reporter has no warehouse network or data tools. Cartographer's lake snapshot is the explicit
exception to HTTP-only agent data access. SellerOS has no proxy environment in Compose.

## End-to-end data flow

```mermaid
flowchart LR
  sim[Simulator S8 world] --> lake[Hourly Parquet + partition_status]
  lake --> warehouse[eye-data DuckDB snapshot]
  warehouse --> complete{Complete hour?}
  complete -->|no: repair on later publication| lake
  complete -->|yes: Core enqueues DBOS data-hours| detect[Sentinel scans + triages]
  detect -->|watch / defer / ignore / attach| later[Persist decision; future tick]
  detect -->|investigate| investigate[Detective evidence + hypotheses]
  warehouse -->|deterministic evidence| investigate
  investigate --> challenge[Skeptic; optional specialists / SellerOS]
  challenge --> investigate
  investigate --> report[Reporter grounded summary]
  report --> incident[Core persists incident + trail]
  incident --> human[Human reviews in web]
  human -->|optional exact approval| revert[Core forwards SellerOS price revert]
  revert -->|shared seller state read by sim| sim
  incident --> recovery[Later complete-hour monitoring]
  recovery -->|default: 3 ticks with absolute z below 2| recovered[Recovered]
  recovered -->|human + review note only| closed[Closed]
```

The campaign is a planned effect, not proof of innocence. S8's partial first hour is
repaired by republishing; no agent reads private `scenario_log`. A revert is optional
and cannot itself prove recovery. Core monitoring starts paused in default Compose;
resume it with a configured model, or use the scripted offline Compose override.

## S8 incident sequence

This is a representative successful investigation, not a promise that every model run
delegates identically or confirms a root cause. Auth/card discovery and repeated tool
calls are abbreviated; DBOS persists remote call checkpoints, not private model histories.

```mermaid
sequenceDiagram
  actor Human
  participant Sim as sim
  participant Lake as Public Parquet lake
  participant Data as eye-data
  participant Core as eye-core / DBOS
  participant Sentinel as sentinel
  participant Detective as detective
  participant Skeptic as skeptic
  participant Experts as eye-specialists / selleros
  participant Reporter as reporter
  Human->>Sim: HTTP inject S8 (demo / scenario control)
  Note over Sim,Lake: Planned coupon + delayed seller typo, partial hour repaired later
  Sim->>Lake: Publish facts then partition_status
  Data->>Lake: Refresh DuckDB landing snapshot
  Core->>Data: HTTP observe complete hour
  Note over Core: Enqueue durable data-hours workflow in Postgres
  Core->>Sentinel: A2A detect
  Sentinel->>Data: HTTP scans, freshness, baselines, plans
  Sentinel-->>Core: Typed decisions + evidence
  opt Decision is investigate
    Core->>Detective: A2A investigate with budgets and incident context
    Detective->>Data: HTTP graph, segments, attribution, price evidence
    Detective->>Skeptic: A2A challenge planned-effect explanation
    Skeptic->>Data: HTTP independent checks
    Skeptic-->>Detective: Typed challenge result
    opt Model-selected delegation
      Detective->>Experts: A2A scoped question
      Experts-->>Detective: Measured findings / seller intent
    end
    Detective-->>Core: Investigation + verdict + trail
    Core->>Reporter: A2A report grounded in investigation
    Reporter-->>Core: Title and summary (or deterministic fallback)
    Core-->>Human: HTTP / SSE web incident + trail
  end
  opt Human approves exact seller price revert
    Human->>Core: HTTP decision for incident / change
    Core->>Experts: HTTP idempotent approved action to selleros
    Note over Experts,Sim: Shared seller-data reflects restored state in simulator
  end
  loop Later monitoring ticks
    Sim->>Lake: Publish subsequent hours
    Core->>Data: HTTP recovery measurements
  end
  Note over Core: Measured recovery, default three qualifying ticks
  Human->>Core: HTTP close recovered incident with review note
```

Source anchors: [Compose](../../compose.yaml),
[workflow](../../packages/orchestrator/src/eye/orchestrator.py),
[warehouse landing](../../packages/data/src/eye/warehouse/landing.py),
[Detective](../../packages/agents/src/eye/agents/detective.py),
[nginx](../../packages/web/nginx.conf), and the service specs below.

## Runtime boundaries

1. `sim` owns private world state and continuously publishes hourly Parquet partitions.
2. `eye-data` reads the lake, computes metrics/baselines/attribution, and stores the metric graph.
3. `eye-core` observes completed hours and enqueues DBOS work. Sentinel selects signals,
   Detective investigates (delegating to Skeptic and specialists), Reporter writes the result.
4. Core owns incidents, approval records, kill switches and audit history. Only measured recovery
   can lead to human closure.
5. `web` serves React and proxies `/api/`; MCP and the demo CLI use Core over HTTP.
6. Postgres holds workflow/control state; Grafana LGTM receives telemetry. Agents use the egress proxy.

Five core A2A services expose `detect`, `investigate`, `challenge`, `report`, `map`.
The specialists service exposes Growth, Marketplace and Discovery; SellerOS is a separate application.
See [Compose](../../compose.yaml) for networks, mounts and entrypoints.

Core does not import agent implementations. Contract validation, remaining budgets, incident context
and trace context cross service boundaries; private model message histories do not.
Core agents and specialists execute PydanticAI agents through the shared `agent_runtime` harness.
Core and specialist prompts ship as package resources next to their implementations.

See [deployment](deployment.md), [orchestrator](orchestrator.md) and [production criteria](agent-criteria.md).
