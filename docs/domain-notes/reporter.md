# Reporter: grounded incident narrative

Implementation: `packages/agents/src/eye/agents/reporter.py`; A2A skill: `report`.
Turn the supplied investigation into a concise title and summary: what moved, affected scope,
measured impact, verdict, and unresolved uncertainty.

Use only exact numbers already present in the investigation/display payload.
The output validator rejects new numbers; a deterministic template handles failed generation.
Reporter has no data/action tools and does not conduct another investigation.

Do not turn a hypothesis into confirmation, a recommendation into an executed action,
or recovery into closure. Core measures recovery; only a person closes after review.
See [core agents](../specs/core-agents.md) and [orchestrator](../specs/orchestrator.md).
