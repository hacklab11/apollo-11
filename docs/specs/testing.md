# Testing and evaluations

Normal tests, PR checks and scripted Compose gates never call real models.
Use PydanticAI `TestModel`/`FunctionModel`, mock transports or recorded fixtures.
Offline agent/runtime fixtures set `models.ALLOW_MODEL_REQUESTS = False`.
CI excludes `live` tests; only mocked compatibility tests should scope an explicit request override.

## Local/CI quality gate

```bash
uv run ruff check . && uv run ruff format --check .
uv run lint-imports
uv run vulture
uv run pytest -m "not live" -q
uv run python scripts/agent_evals.py
```

Vulture scans workspace/simulator source with explicit framework/public-API references in
`scripts/vulture_whitelist.py`. It is name-based unused-code detection, not proof of whole-program
reachability. Keep confidence/whitelist narrow; import-linter independently enforces package boundaries.
Frontend CI also checks lint, tests, build and generated OpenAPI types.

## Offline Compose integration

Use a fresh project with no previous incidents (choose a new name for each independent run):

```bash
export COMPOSE_FILE=compose.yaml:compose.e2e.yaml
export COMPOSE_PROJECT_NAME=eye-s8-check
export COMPOSE_DISABLE_ENV_FILE=1
docker compose up -d --build --wait --scale detective=3 && ./scripts/smoke.sh && ./scripts/e2e-s8.sh
docker compose down
```

`compose.e2e.yaml` removes provider keys and routes requests to `tests/e2e/provider.py`.
The gate checks three Detectives, egress restrictions, S8 investigation/report,
SellerOS human approval, measured recovery, human close and queued tenant backpressure.
Plain Compose smoke checks startup without keys; it is not the scripted S8 investigation gate.

## Agent evals

`scripts/agent_evals.py` uses pydantic-evals datasets under `tests/evals/`, offline by default.
Cases exercise production agents with scripted responses and quality/end-state evaluators;
replay is not a live-model score. The separate nightly workflow uses `--live` and
`OPENROUTER_API_KEY`, iterates configured model/fallback choices, and caps shared usage.
Do not enable live calls in routine tests. See [production criteria](agent-criteria.md) for gaps.
