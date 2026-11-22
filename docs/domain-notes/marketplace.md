# Marketplace: offers, availability and seller mix

Implementation: `packages/specialists/src/eye/specialists/marketplace.py`;
A2A endpoint: `/marketplace`. This read-only specialist uses the legacy LLM runtime.

Track offer price/stock and Buy Box holders by SKU, seller and tier.
A change in category GMV share is not the same as a count of Buy Box wins:
check the metric definition and separate rate, volume and seller mix.
Healthy totals can hide small-third-party displacement.

Stockouts, purchase-order delivery, fulfillment capacity and recently joined sellers
provide alternative supply explanations. Compare their timing and unaffected controls.
A low price is an observation, not proof of repricer intent; SellerOS owns private intent/cost data.
Use offers, buybox, stock, purchase_orders, capacity, new_sellers, series and changes tools.
Never mutate an offer from this agent.
See [specialists](../specs/specialists.md) and [metric definitions](metric-descriptions.md).
