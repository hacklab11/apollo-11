# Architecture

Eye is a monitoring and incident workspace: deterministic tools measure, agents interpret,
and humans approve seller actions and close recovered incidents.

## Runtime boundaries

1. `sim` owns private world state and continuously publishes hourly Parquet partitions.
2. `eye-data` reads the lake, computes metrics/baselines/attribution, and stores the metric graph.
3. `eye-core` observes completed hours and enqueues DBOS work. Sentinel selects signals,
   Detective investigates (delegating to Skeptic and specialists), Reporter writes the result.
4. Core owns incidents, approval records, kill switches and audit history. Only measured recovery
   can lead to human closure.
5. `web` serves React and proxies `/api/`; MCP and the demo CLI use Core over HTTP.
6. Postgres holds workflow/control state; Grafana LGTM receives telemetry. Agents use the egress proxy.

Five core A2A services expose `detect`, `investigate`, `challenge`, `report`, `refine_graph`.
The specialists service exposes Growth, Marketplace and Discovery; SellerOS is a separate application.
See [Compose](../../compose.yaml) for networks, mounts and entrypoints.

Core does not import agent implementations. Contract validation, remaining budgets, incident context
and trace context cross service boundaries; private model message histories do not.
Legacy specialist inference still uses `eye.llm.runtime/client`; it is not the core-agent harness.

See [deployment](deployment.md), [orchestrator](orchestrator.md) and [production criteria](agent-criteria.md).
