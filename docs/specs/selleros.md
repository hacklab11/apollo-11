# SellerOS

Independent package: `packages/selleros/selleros/`; entrypoint: `selleros`, port 8300.
The service exposes A2A seller investigation plus human-approved action endpoints.
It shares leaf contracts and `agent_runtime`, not Eye implementation modules.

`agent.py` uses PydanticAI with read-only tools scoped to the requested seller/SKUs:
repricer state, costs/margins, last-24-hour price history, supplier ETA and deterministic price-intent checks.
Default caps: six steps, 20 tools, 16,000 tokens. A scope failure must not disclose another seller's data.

The model may propose a response/action but cannot execute a seller mutation.
`actions.py` and `server.py` validate the exact approved price-revert contract and current seller state;
the restore only applies to the approved price change, not a later edit.
The seller store records idempotent results. Core owns the human decision and forwards
the approved operation; simulator ingestion reflects the resulting seller state.

`SELLEROS_DATA_DIR` selects private seller storage, shared with the simulator in Compose.
Do not commit this data, expose private costs as platform metrics, or grant action tools to
read-only investigative agents. Broader action coverage is tracked in [production criteria](agent-criteria.md).
