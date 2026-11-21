# T05 Scenario S6: Black Friday cascade

Owner: Dev 1 · Depends on: T04 · Blocks: Checkpoint 3

## Goal
The cross-actor feedback loop emerges from the seller rules after a planned 1P price cut during Black Friday, plus a hijacker and lagged refunds.

## Read first
`docs/specs/00-overview.md` §3, `docs/specs/04-scenarios.md` S6, `docs/specs/03-simulator.md` §4.

## Files you own
`src/eye/sim/scenarios.py` (S6 parts only), `config/scenarios.yaml` (S6 section), `tests/sim/test_scenario_s6.py`. Tuning edits in `src/eye/sim/market.py` and `ads.py` are allowed if they keep T02/T03 tests green (list them in the PR).

## Steps
1. Inject: the I1 effects at T0, the 1P price cut at T0+2h (with its planned change_log row), S-99 activation at T0+12h (no change_log row; `sellers.joined_ts`).
2. Do not script prices. The repricers, Buy Box, stock, capacity, and bids must produce the cascade.
3. Tune parameters until the targets in `04-scenarios.md` "Tuning targets at T0+24h" hold for seed 42 and for seeds 7 and 123.
4. If after 2 hours of tuning the cascade does not appear: implement the fallback (cut item 7 in the plan): drive the same metrics with scripted multipliers on the same timeline, and say so in the PR.

## Done when
```bash
uv run pytest tests/sim/test_scenario_s6.py -m "not live" -q
uv run eye-sim generate --days 28 --seed 42 --play "S6@0" && uv run eye-sim export --format csv --out data/export/s6
```
Test asserts every tuning target at T0+24h for 3 seeds; SellerOS file shows S-03, S-07, S-12 below cost on at least one SKU at T0+12h; baseline without S6 still passes T02 tests.

## Out of scope
Agent behaviour.
