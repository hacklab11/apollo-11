# Agent runtime

`packages/agent_runtime/src/agent_runtime/` is the shared PydanticAI harness,
not an agent/router. `runner.py:run_agent` returns typed outcomes and usage; `guard.py`
enforces checks before model/tool execution.

- `limits.py:RunLimits` defaults to 20 model steps, 40 tool calls, 200,000 tokens,
  USD 0.10, 120 seconds, and a repeated-call threshold of three. Agents may tighten these.
- `PriceTable` prices usage from pinned configuration and rejects unpriced paid routes.
  Cost is checked after responses; it is not a provider-side prepaid reservation.
- `Budget` propagates remaining tokens, USD, deadline, depth and fan-out.
  Delegations split available resources and refuse excess depth/fan-out.
- `guard.py` detects repeated identical tool/argument calls, checks kill switches,
  and uses idempotency stores for marked side effects.
- `results.py` shares explicit done/needs_input/failed/gave_up contracts.
  Limits/loops become gave_up; kill/model failures map to failed.
  Deferred approval tools become needs_input with pending calls.
- `resilience.py` supplies outbound tracing, timeouts, safe retries and per-host circuits.
  Defaults: three attempts, full-jitter exponential backoff from 0.2 to 5 seconds;
  circuit opens after five consecutive failures and probes after 30 seconds.
  Non-idempotent requests are not retried without an idempotency key.
- `idempotency.py` hashes run, tool and canonical arguments. The in-memory store is process-local;
  durable service executors must provide their own storage/unique constraint.
- `models.py` builds guarded fallback models; `tracing.py` instruments runs and redacts PII.

The harness does not itself checkpoint every model/tool call into DBOS.
Orchestrator checkpoints are at workflow/A2A-step boundaries; see [criteria and gaps](agent-criteria.md).
Specialists still use the separate `eye.llm.runtime` loop.
