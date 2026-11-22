# Deployment and Compose

[compose.yaml](../../compose.yaml) is the local service topology. Run
`cp .env.example .env` then `docker compose up -d --build --wait`.
Host UI: http://localhost:8080; Grafana: http://localhost:3000; simulator: port 8100.
MCP and SellerOS use loopback host-port ranges; agents and data are internal.
Compose mounts `config/compose.yaml` as the application settings file; model credentials come from environment.

## Networks and state

- `eye`: edge/control/infrastructure. `agents` and `warehouse` are internal networks.
- Agents have no direct internet route; HTTP(S) goes through Squid.
  `config/squid/squid.conf` restricts methods/CONNECT ports and
  `allowlist.txt` lists permitted destinations. Internal service calls bypass the proxy.
- Reporter has no warehouse network; investigative agents can call data.
  Cartographer also mounts the public lake read-only for SQL validation.
- Named volumes persist simulator world/lake, warehouse, incident data, SellerOS and Postgres state.
  `docker compose down` retains them; deleting volumes is destructive.
- `eye-data` does not receive the shared `.env` provider credentials.
  Private simulator world state is not mounted into investigative agents.

## Scaling and admission

`docker compose up -d --scale detective=3` adds worker service replicas without host-port collisions.
DBOS in Core, not an HTTP request handler, executes queued hours.
Defaults in `OrchestratorSettings`/Compose: global concurrency 2, tenant concurrency 1,
60 admissions per 60 seconds and at most 100 queued per tenant.
Root budgets default to 100,000 tokens, USD 0.50 and 600 seconds;
maximum delegation depth is 2 and fan-out 3.
These are application defaults, not a shared-production isolation guarantee.

Compose is a local deployment, not a complete production security/SLO design.
Use the [offline override](testing.md) for credential-free integration tests.
See [criteria](agent-criteria.md) for implemented enforcement and explicit gaps.
