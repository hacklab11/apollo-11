# AGENTS.md — Eye of the System

Read [README.md](README.md), the [documentation index](docs/README.md), and the relevant current specs before editing.

## Workspace ownership

| Directory | Owns |
|---|---|
| `packages/contracts` | `eye.contracts` v1, `contracts_v2`, shared configuration |
| `packages/data` | schema, registry, graph, deterministic tools, warehouse HTTP API/client |
| `packages/agent_runtime` | shared PydanticAI execution guards, budgets, resilience and tracing |
| `packages/llm` | shared core-agent and specialist prompt resources only |
| `packages/agents` | five core agents and their A2A service |
| `packages/orchestrator` | DBOS orchestration, directory, incident/control stores and public API |
| `packages/specialists` | Growth, Marketplace, Discovery A2A service |
| `packages/selleros` | independent seller-side agent, data and approved actions |
| `packages/mcp` | MCP facade over Core |
| `packages/demo` | standalone HTTP-only `demo` CLI |
| `packages/web` | React/TypeScript incident workspace and nginx edge |
| `sim` | continuous simulator, S8 plugin and public Parquet lake |

`config/` owns settings, registries and operational provisioning; `tests/` owns cross-package tests.
Current specifications are in `docs/specs/`; archived cards are reference material, not file-ownership rules.

## Boundaries and code rules

- Tools compute; agents decide. Never invent numerical evidence in model text.
- Reuse shared contracts. v1 remains live; v2 imports shared v1 shapes.
- Import-linter in root `pyproject.toml` is authoritative: contracts/config are foundational;
  data cannot import agents or orchestration; agents/specialists cannot import orchestration.
- Core agents are independent. Orchestrator calls them over A2A, never by importing implementations.
- Production has no static simulator imports. Embedded simulation is selected dynamically at the data-source boundary.
- `agent_runtime` may share only `eye.contracts_v2` from Eye; SellerOS shares leaf v2 contracts
  and the runtime, not Eye application modules. Eye never imports SellerOS.
- Demo CLI is independent of application packages; application packages never import it.
- No agent tool may read `scenario_log` (ground truth).
- Type all boundaries; validate API/A2A/MCP/tool payloads with Pydantic. Tool summaries are concise (at most 300 characters).
- Async for I/O; synchronous DuckDB/numeric code is appropriate. Make the smallest correct change.
- Never put secrets in code, tests, fixtures, prompts, logs or commits. Do not commit `.env`, `data/` or `packages/selleros/data/`.
- Bind host-facing services to loopback; container services may bind `0.0.0.0` on private networks.
- Keep logs free of raw prompts and secrets. Do not change auth implementation as part of unrelated work.

## Pinned stack

Use Python 3.13 via `uv`, not system Python. Workspace manifests/lockfile pin:
PydanticAI (`pydantic-ai-slim`) 2.66.0, DBOS 3.6.0, a2a-sdk 1.3.1, MCP 2.3.0,
DuckDB 2.0.1, FastAPI 0.165.0, Pydantic 2.14.4, httpx 0.28.1,
Logfire 6.0.0, Ruff 0.17.6, pytest 9.2.0, import-linter 2.11, Vulture 2.16,
and pydantic-evals 2.66.0. React is 19.2; TypeScript is 5.9.
Read the installed SDK's documentation before using its API; do not guess method names.

## Commands

```bash
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run lint-imports
uv run vulture
uv run pytest -m "not live" -q
uv run python scripts/agent_evals.py
```

Keep Vulture's explicit whitelist narrow: framework callbacks and supported APIs only,
not broad ignores to hide orphan code. Frontend: `npm ci`, `npm run lint`, `npm test`,
`npm run build` in `packages/web`; `npm run generate:api` regenerates API types.

Offline Compose gate, from a fresh project (prior incidents make the S8 harness fail):

```bash
export COMPOSE_FILE=compose.yaml:compose.e2e.yaml
export COMPOSE_PROJECT_NAME=eye-s8-check
export COMPOSE_DISABLE_ENV_FILE=1
docker compose up -d --build --wait --scale detective=3 && ./scripts/smoke.sh && ./scripts/e2e-s8.sh
docker compose down
```

Choose a new project name for a fresh run. Plain Compose is not the offline S8 gate:
the override supplies a scripted provider and test settings.

## No real model calls in tests

Normal tests and PR evals must never call real model providers. Use PydanticAI `TestModel`,
`FunctionModel`, scripted transports or recorded fixtures. Keep
`pydantic_ai.models.ALLOW_MODEL_REQUESTS = False` in offline harnesses; do not bypass it
with a live transport. Mock-transport provider compatibility tests may explicitly scope an override.
Runtime/agent test fixtures guard model requests; CI runs `pytest -m "not live"` and offline evals.
Only the separate nightly live-eval workflow opts in to paid providers with `agent_evals.py --live`,
a capped shared budget and provisioned credentials. Do not run live tests for routine PR verification.

## Pull requests

- Branch: `task/<id>-<slug>`.
- Title: `<id>: <short title>`.
- Description sections: **Summary**, **Files changed**, **Done-when evidence** (paste check output),
  **Assumptions**, **Spec issues**, **Not done**.
- No screenshots.
- State contradictions and unverified behavior plainly. Prefer reversible assumptions and record them;
  do not silently change a spec or expand scope.
