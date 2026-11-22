# Growth: acquisition, auctions and promotions

Implementation: `packages/specialists/src/eye/specialists/growth.py`;
A2A endpoint: `/growth`. This read-only specialist uses PydanticAI through `agent_runtime`.

Compare ad spend with cumulative planned spend, not a flat hourly budget.
Auction bidders, bids, CPC and slot fill distinguish demand/competition from pacing.
Marketing spend and attributed sessions should be compared by country/channel.
Platform-funded discounts are a platform cost; they are not a seller's manual price.

In S8, the planned electronics coupon day can explain some platform-discount movement.
It does not by itself explain a below-cost seller price or all margin loss.
Return scoped measurements from ads/pacing, ads/auction, marketing, discounts, metric series
and change-log tools. A coincident campaign is not automatically causal.
See [specialists](../specs/specialists.md).
