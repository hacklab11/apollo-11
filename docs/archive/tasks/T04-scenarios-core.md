# T04 Scenarios S1–S5, S7, I1, I2

Owner: Dev 1 · Depends on: T02, T03 · Blocks: T05, T23

## Goal
Inject and clear each scenario with its change-log rows, effects, and decoys, so the data matches the scenario bible.

## Read first
`docs/specs/04-scenarios.md` (all except S6 details), `docs/specs/03-simulator.md` hooks.

## Files you own
`src/eye/sim/scenarios.py`, `config/scenarios.yaml`, `tests/sim/test_scenarios.py`.

## Steps
1. `ScenarioManager` with `inject(id, at_ts=None)`, `clear(id)`, `active()`, `timeline()` (for the orchestrator), writing `scenario_log`.
2. Implement each scenario through the hooks from T01–T03 only (no special cases inside demand/market code): S1, S2 (with backdated release and PO history rewrite), S3, S4, S5, S7, I1, I2 (with the 6 h backfill). Make the I2 late-partition hook take countries, loaded share and backfill delay as parameters: S8 (T27) reuses it with both countries, share 0.5 and a 1 h backfill.
3. Each scenario's decoys from the bible.
4. Parameters in `config/scenarios.yaml` with a comment per value.
5. `eye-sim generate --play` now works for these ids.

## Done when
```bash
uv run pytest tests/sim/test_scenarios.py -m "not live" -q
uv run eye-sim generate --days 28 --seed 42 --play "I1@0,S7@3" && uv run eye-sim stats
```
One test per scenario, checking the data signature after 3–6 h (no LLM):
- S1: ADV-3 `spent_today/daily_budget` ≥ 0.95 by 10:00; others ≤ 0.6.
- S2: home `in_stock_rate` −30% or more; home searches z within ±2; T05 delivery delayed (decoy).
- S3: DE search conversion −40% or more; FR within ±5%; searches z within ±2.
- S4: feed CTR up ≥ 10%; feed purchase rate −25% or more; unavailable exposure ≥ 30%.
- S5: orders up; contribution margin down; marketing + discounts ≥ 85% of the drop.
- S7 with I1: web and android `cart_to_checkout_rate` −80% or more; iOS within ±5%; PSP-2 success rate normal; GMV below I1 expectation by ≥ 30%.
- I1 alone: sessions +35..45%; no metric breaks its baseline except planned ones.
- I2: FR completeness 0.4 for 3 h; after 6 h values backfilled.
- `clear()` returns metrics to baseline within 3 h (except lagged effects).

## Out of scope
S6 (T05). S8 and I3 (T27).
