# Specialists and A2A

`packages/specialists/src/eye/specialists/` exposes Growth (`/growth`),
Marketplace (`/marketplace`) and Discovery & Experience (`/discovery`) on port 8200.
Each returns a typed specialist response, evidence and usage through A2A.
Like core agents, specialists use PydanticAI through `agent_runtime.run_agent`, with
typed tools, explicit end states, pinned pricing and an eight-model-step cap.

| Specialist | Read-only warehouse evidence |
|---|---|
| Growth | ad pacing, auction health, marketing channels, platform-funded discounts |
| Marketplace | offers/Buy Box, stock, purchase orders, capacity, recently joined sellers |
| Discovery | search quality, feed exposure, checkout funnel, payments, client errors, component versions |

Tools in `specialists/tools.py` call typed domain routes from `eye.warehouse.domain`.
Specialists do not mutate platform/seller state or consume scenario ground truth.
A plausible domain story without measurements is not a confirmed cause.

Core discovers configured A2A services through `orchestrator/src/eye/directory.py`;
the client implementation is `packages/agents/src/eye/a2a_client.py`.
Cards advertise skills; requests carry incident context, trace context and remaining budgets.
Core-agent serving is implemented in `agents/service.py`; per-agent private conversation state
does not cross the boundary. SellerOS is an [independent A2A service](selleros.md).
Agent-card/auth work is being added separately; this document does not specify that work.
