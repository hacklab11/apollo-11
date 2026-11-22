# DBOS orchestrator and incident control

`packages/orchestrator/src/eye/` owns the deterministic control plane.
Entrypoint `eye-core` serves FastAPI on port 8000. `main.py` selects a remote
`ServiceClock`/warehouse client when `EYE_DATA_URL` exists; otherwise it uses the embedded
[data-source adapter](data.md).

`orchestrator.py` observes complete warehouse hours, enqueues the `data-hours` DBOS queue
and calls A2A skills in order: detect → investigate → report.
Detective handles its own challenges/delegation. Per-tenant admission/concurrency limits,
root budgets, kill checks, trace context and incident context accompany execution.
Agent-directory URLs come from `a2a` settings, not agent implementation imports.

DBOS workflows and A2A call steps persist in Postgres selected by `EYE_DATABASE_URL`;
local fallback is SQLite control state.
`EYE_DBOS_APPLICATION_VERSION` pins workflow compatibility (default `eye-orchestrator-v1`).
Keep it stable across compatible deployments so pending workflows recover; drain old-version
runs before changing it for an incompatible workflow update.
`control.py` stores kill switches, approval decisions and hash-chained audit records. DBOS checkpoints A2A steps;
it does not checkpoint each private model/tool call inside a remote agent.

`incidents/store.py` persists incidents/trails in DuckDB, matches related symptoms and
updates lifecycle. A business verdict is separate from operational closure:
investigating → open → mitigating → recovered → closed.
Default recovery requires three consecutive monitoring ticks with absolute z below 2.
Only a person can close a recovered incident with a review note; agents never close.
Human-approved seller reverts bind the exact incident/change and use a durable idempotent path.

Run requests enqueue, not execute inline. Tenant over-limit work remains queued or is rejected
by admission bounds. Queue settings and budgets are listed in [deployment](deployment.md).
Core monitoring can pause independently of the continuously advancing simulator.
