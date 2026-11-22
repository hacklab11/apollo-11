# Contracts and configuration

Implementation: `packages/contracts/src/eye/{contracts.py,contracts_v2.py,config.py}`.

- v1 owns shared metric, graph, anomaly, evidence, investigation and trail shapes and remains in use.
- v2 adds tenant-scoped requests/results, metadata, budgets, explicit agent end states,
  incident lifecycle and approval contracts; it imports shared v1 shapes.
- `AgentResult[T]` distinguishes `done`, `needs_input`, `failed`, `gave_up`, with usage,
  result/question/reason as appropriate. A2A maps these to completed, input-required or failed.
- `TaskMetadata` carries tenant, incident context, budget, trace context and delegation controls.
  Validate incoming and outgoing boundaries rather than redefining wire models locally.
- Lifecycle transitions are explicit: investigating → open → mitigating → recovered → closed,
  with allowed recovery/reopening transitions encoded in `INCIDENT_TRANSITIONS`.
  Closure is a person-authored record, not an agent verdict.
- Price-revert approval binds an incident, seller, SKU, timestamp, observed price and restore price.
  Stable contract-derived idempotency keys prevent repeating the approved mutation.

`load_settings()` reads YAML settings and scenario parameters; Compose mounts
`config/compose.yaml` as `config/settings.yaml`. Service-specific environment variables
(such as `EYE_DATA_URL`, `EYE_DATABASE_URL` and `SIM_LAKE_DIR`) are read by the entrypoints;
there is no universal environment-to-settings mapping.

Keep contracts foundational: no imports from data, agent, orchestration or SellerOS implementations.
See [data](data.md) for adapter selection and [agent runtime](agent-runtime.md) for enforcement.
