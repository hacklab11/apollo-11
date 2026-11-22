# Agent production criteria

This checklist maps requirements to current code, not to aspirational behavior.
Paths are relative to the repository root. “Partial” means the complete criterion is **not yet** met.

| Criterion | Implementation / gap |
|---|---|
| Steps, tools, tokens, cost, wall-clock caps | `packages/agent_runtime/src/agent_runtime/{limits,guard,runner}.py`; per-agent limits tighten defaults. Cost checked after responses. |
| Explicit end states | `packages/contracts/src/eye/contracts_v2.py:AgentResult`, runtime `runner.py`, core A2A service mapping. |
| Repeated-call loop detection | Runtime `guard.py` hashes tool/args; default threshold three. |
| Run/tenant/system kill switch | Runtime `kill_switch.py`, orchestrator `control.py` and API run-control routes. |
| Checkpoint/resume every model/tool call | **Not yet**. `orchestrator.py` checkpoints workflows/A2A steps; private agent calls are not DBOS checkpoints. |
| Idempotent side effects | Runtime `idempotency.py`; control approvals and SellerOS `data.py/actions.py` persist action outcomes. Process-local runtime store alone is not durable. |
| Timeouts, retries, circuits | Runtime `resilience.py`, model guards and A2A/data/MCP clients; safe retry requires idempotence. |
| Model fallback chain | Runtime `models.py`, core `agents/models.py`, settings model/fallback lists. Specialist legacy runtime is separate. |
| Least-privilege short-lived credentials | **Not yet covered here**; auth work belongs to the sibling change. |
| Tool-layer human approval | Core `control.py`, API approval routes, SellerOS `server.py/actions.py` enforce exact price reverts; incident close is human-only. Generic approval coverage for every action: **not yet**. |
| Untrusted content separated from authority | Tagged request/tool data and scoped read-only seller tools; mutations run in approved executors. Uniform enforcement across all legacy paths: **not yet**. |
| Schema validation | Shared Pydantic contracts, warehouse operations, A2A request/result and tool schemas. Uniform `extra="forbid"` across every legacy shape: **not yet**. |
| Sandbox, egress, no secrets in context | Data `tools/sqlguard.py`, internal Compose networks, Squid allowlist, environment credentials; privacy/egress tests. Not an arbitrary-code sandbox. |
| End-to-end traces and cost metrics | Runtime `tracing.py`, directory/A2A metadata, OTLP → LGTM, PydanticAI instrumentation. |
| Replayable I/O with capture-time PII redaction | Partial: `tracing.py` scrubs telemetry; legacy LLM record/replay exists. Universal persisted redacted per-step replay: **not yet**. |
| Alerts | `config/grafana/alerting/rules.yaml`: cost, loop, failure and needs_input rates. Production notification routing: **not yet**. |
| Immutable audit | `packages/orchestrator/src/eye/control.py` appends hash-chained records. External tamper-proof/WORM retention: **not yet**. |
| Per-model eval gates | `scripts/agent_evals.py`, `tests/evals/`, CI offline evals and separate capped nightly live workflow. |
| Production failures → regression capture command | **Not yet**; no `eye eval capture <trace_id>` command. |
| Pinned prompts/models/tools | Manifests/lock pin libraries; model/fallback/price settings and typed contracts are explicit. Universal per-run prompt-hash/tool-version provenance: **not yet**. |
| Shadow/canary and one-step rollback | **Not yet**; editing model settings is not a canary/rollback system. |
| Queued runs, rate limits and backpressure | DBOS queue/admission in `orchestrator.py`, `OrchestratorSettings`, tenant queue e2e test. |
| Tenant isolation | Partial: tenant contracts, metadata, queue partitions and control checks. Uniform row-level isolation across warehouse, incidents and all services: **not yet**. |
| Operational SLOs | End-state metrics exist; defined/enforced success, latency and uptime SLOs: **not yet**. |
| Human handoff | Typed needs_input/pending approvals, Core approval records and web review flows. General durable task inbox for every gave_up/needs_input: **not yet**. |
| Agent authentication | **Not yet covered here**; agent-card/auth work is being added separately. |
| Delegation depth/fan-out | Runtime `budget.py`, typed `Budget`, A2A metadata and configured depth/fan-out bounds. |
| End-to-end budget | Runtime `budget.py/guard.py`, directory calls and delegated requests carry remaining tokens, USD and deadline; responses charge usage. |

Specialists retain `eye.llm.runtime/client`; do not assume every runtime criterion is uniformly
enforced there. Core-agent and SellerOS checks are covered by `packages/agent_runtime/tests`,
`tests/agents`, `tests/test_orchestrator.py`, `tests/test_control_plane.py`,
`packages/selleros/tests` and the scripted Compose gate.
