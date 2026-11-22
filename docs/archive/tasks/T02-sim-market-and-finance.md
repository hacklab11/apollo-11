# T02 Simulator: offers, repricers, Buy Box, stock, purchase orders, finance, SellerOS data

Owner: Dev 1 · Depends on: T01 · Blocks: T04, T05, T17

## Goal
Marketplace dynamics between big and small players, money accounting, and the seller-private data file for SellerOS.

## Read first
`docs/specs/simulator.md` §2.2–2.3, §4, §6, §9. `docs/specs/selleros.md` §3.

## Files you own
`src/eye/sim/market.py`, `src/eye/sim/finance.py`, `src/eye/sim/selleros_export.py`, `tests/sim/test_market.py`, `tests/sim/test_finance.py`. Small edits in `src/eye/sim/demand.py` to use the Buy Box (explain in the PR).

## Steps
1. `market.py`: Buy Box rule (§4.1), repricers with `fixed`/`match`/`beat` + recovery + floors (§4.2), 3P stock and restock with FBA capacity and blocked units (§4.3), 1P purchasing with POs and deliveries (§4.4), seller bid escalation flag for T03 (§4.5: expose `out_of_stock_hours[seller, sku]`).
2. Write `offers_hourly`, `purchase_orders`, `fba_capacity_daily`.
3. Orders go to the Buy Box offer; stock decreases; out of stock → substitution rule (§3.4).
4. `finance.py`: fee revenue, 1P margin, platform discount (0 for now; scenarios set it), fulfillment cost, lagged refunds with per-seller probability (§6). Write all money columns and `refunds_hourly`.
5. `selleros_export.py`: write `external/selleros/data/sellers.json` (format in 11 §3) after backfill and after every `step()` (atomic write).
6. Register hooks into the T01 engine.

## Done when
```bash
uv run pytest tests/sim -m "not live" -q
uv run eye-sim generate --days 28 --seed 42 && uv run eye-sim stats
```
Tests: 03 §9 items 2 (price index drift < 1% over 7 days), 4 (contribution identity), 7 (no S-99 offers); Buy Box picks the expected offer in a unit case; restock respects capacity; `sellers.json` has 24 small sellers and no private data in DuckDB (no cost/floor columns).

## Out of scope
Ads auction, scenarios.
