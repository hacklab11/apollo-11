# SellerOS: private evidence and approved actions

Implementation: `packages/selleros/selleros/`; independent A2A service, not a platform specialist.
Read tools are scoped to the requested seller and SKUs: repricer state, unit costs/margins,
recent price history, supplier ETA and deterministic price-intent checks.

Distinguish a deliberate price move, repricer behavior and a manual below-cost change.
Compare observed/current price, previous price, unit cost and timestamp rather than assuming
a cheap offer is a typo. Private seller data must not leak into another seller's response.

The investigative model holds read-only tools. Reverting a manual price requires human approval
of the exact incident/seller/SKU/change; the action executor rechecks current state and idempotency.
A proposed revert is not an executed revert. A later seller edit must not be overwritten
by an approval for an earlier change. Core, not the seller model, owns incident closure.
See [SellerOS](../specs/selleros.md) and [production criteria](../specs/agent-criteria.md).
