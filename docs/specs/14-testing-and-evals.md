# 14 Testing and evals

Tasks: every task has its own tests (listed in its card). T23 builds the scenario eval harness.

## 1. Levels

| Level | Tool | LLM | Runs |
|---|---|---|---|
| Unit | pytest | none (fake client) | every PR (`uv run pytest -m "not live"`) |
| Replay | pytest + recorded `tests/fixtures/llm/*.jsonl` | replay | every PR |
| Agent eval | pydantic-evals + `tests/evals/cases/` | offline FunctionModel | every PR, score 1.000 per agent/model |
| Nightly agent eval | `scripts/agent_evals.py --live` | OpenRouter, capped | schedule / workflow_dispatch, opt-in only |
| Live eval | `scripts/eval_scenarios.py` | live | by hand at checkpoints (costs money) |
| Manual | checklist in PR | live | UI, MCP in Claude Code, deployment |

Commands (defined in T00, written in `AGENTS.md`):

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest -m "not live" -q
uv run python scripts/agent_evals.py --report data/eval/offline.json
uv run python scripts/eval_scenarios.py --scenario S3 --mode live --record tests/fixtures/llm/s3.jsonl
uv run python scripts/eval_scenarios.py --all --mode replay
```

## 2. Scenario oracle (`tests/oracle/scenarios.yaml`, T23)

S8 owns both the campaign and the typo. S8 and D6 play `S8@0` with `scenario_params: {S8: {typo_delay_hours: 10}}`; D6 adds `fee_per_order` while open and clears S8 through the simulator manager after the SellerOS ack, then expects resolution after 3 normal ticks. `coupon_day` preserves the innocent campaign-only check (`INNOCENT`, no alert; mentions `coupon-day-electronics`, `planned`): it plays S8 with a 24-hour typo delay and checks the first 6 hours. Detection windows start at the typo, not at the coupon campaign. Other historical scenarios remain oracle specifications but return `simulator_available: false` in the S8-only simulator.

Written by P3 from `04-scenarios.md`, one entry per scenario and per combined run (D1–D6):

```yaml
S7:
  play: ["I1@0", "S7@3"]
  run_hours: 6
  max_ticks_to_detect: 2
  expect:
    entry_metrics_any: [gmv, orders, cart_to_checkout_rate, checkout_conversion]
    verdict_any: [CONFIRMED]
    root_node_any: [cart_to_checkout_rate]
    cause_type: release
    segment_contains: {platform: [web, android]}
    must_mention: [checkout-web, v5.8, ios, PSP-2]
    must_not_root_cause: [traffic_change, planned_event]
    evidence_groups_min: 2
    max_incidents_for_scenario: 1
  also:
    I1: {status_any: [innocent], or_no_incident: true}
```

## 3. Eval harness (`scripts/eval_scenarios.py`, T23)

1. Fresh world from the seed (temp dir). Start core in-process (no HTTP needed) with the orchestrator in step mode.
2. Apply `play` at the given hours; step `run_hours` ticks.
3. Collect incidents and trails. For each check in `expect`: pass/fail with the actual value.
4. Print a table and write `data/eval/<scenario>.json`. Exit code 0 only if all checks pass.
5. Also record: ticks to detect, LLM calls, tokens, wall time.

Use the results at checkpoint 2 (`../plan/implementation-plan.md`). A failing scenario leads to a prompt or tool fix, not to a change of the oracle, unless the oracle is wrong (then fix `04-scenarios.md` first).

## 4. Fixtures

- LLM fixtures are recorded from live runs (`--record`). Commit them (they contain no secrets; check before commit).
- Legacy scenario fixtures use prompt-derived replay keys. Missing recordings fail offline replay; never silently fall back to live.
- Wave 6 agent evals use Dataset cases under `tests/evals/cases/`, including recursive `regressions/` discovery. Expected stable decision fields are independent of recorded submissions. Version pins include the current prompt and actual model-facing tool schema hashes, route ID, and matching PydanticAI/pydantic-evals versions. S8 cases capture the finite integration script's responses on small deterministic boundary inputs, not live-model quality measurements.
- `scripts/agent_evals.py --live` is the separate opt-in path used by `nightly-eval.yml`. A shared PydanticAI UsageLimits caps the whole sequential run to 30 provider requests and 100,000 cumulative tokens, with max_tokens=1,024 per request and no SDK retries. This is request-bounded spend, not a USD credit limit. Unit tests never enable real providers. See the root README for commands and secret setup.

## 5. Test data rules

- All test data comes from the simulator with a fixed seed or from small factories in `tests/factories.py`. No hand-made large JSON.
- Tests that need a world use the session fixture `world_7d` (7 days history, seed 42) to keep test time low.
- Unit test run must finish in < 60 s.
