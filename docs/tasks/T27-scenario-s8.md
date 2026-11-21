# T27 Scenarios S8/I3: price typo (live demo)

Owner: Dev 1 · Depends on: T04 (scenario manager, I1, I2 late-partition hook), T02 (`sellers.json`) · Blocks: T23 (D6), T24, rehearsals

## Goal
Implement I3 "Electronics coupon day" and inject and clear S8 "The €9 headphones" so the data matches the scenario bible. This is the scenario of the 3-minute live demo.

## Read first
`docs/specs/04-scenarios.md` S8, I3, I1, I2 and run D6; `docs/specs/15-demo-and-pitch.md` §1; `docs/specs/03-simulator.md` §3.4, §4.2, §7; `docs/specs/11-selleros-external.md` §3.

## Before you start
`ScenarioId` in `src/eye/contracts.py` must contain `"S8"` and `"I3"` (`docs/specs/02-contracts.md` §10). Per `docs/plan/implementation-plan.md` §2 item 5, a developer makes this change by hand on `main`. If either is missing, stop and report it. Do not change `contracts.py` in this PR.

## Files you own
`src/eye/sim/scenarios.py` (S8 and I3 parts only), `config/scenarios.yaml` (S8 and I3 sections), `tests/sim/test_scenario_s8.py`, `tests/sim/test_scenario_i3.py`. The code that writes `external/selleros/data/sellers.json` may get the `source` field (list the file in the PR).

## Steps
1. Implement I3: reuse I1 coupons for 24 h, `scenarios.I3.discount: 0.1`, no session uplift (`campaign_mult = 1.0`), planned `campaign` row `coupon-day-electronics`, scope `{category: electronics}`, coupon-only `expected_effect`.
2. Choose `scenarios.S8.seller_id` for seed 42 by the rule in 04 S8. Write it in `config/scenarios.yaml` with a comment.
3. `inject(S8)`: price × 0.10 on the seller's offers on E01–E10, repricer mode `fixed` on these offers, stock for a 4–8 h sellout (aim 6 h, by 16:00), and the I2 late-partition hook for the current hour still filling (10:00–11:00; Sentinel investigates at 11:00), using both countries, share 0.5, backfill at the next tick. No `change_log` row.
4. Write the price change to the seller's `price_history` with `source: "manual"`. Other entries get `source: "repricer"` (or no `source`).
5. `clear(S8)`: restore the pre-S8 price and repricer mode on the affected offers. Sold stock stays sold.
6. Use the hooks from T01–T04 only. No special cases inside demand or market code.

## Done when
```bash
uv run pytest tests/sim/test_scenario_s8.py tests/sim/test_scenario_i3.py -m "not live" -q
uv run eye-sim generate --days 28 --seed 42 --play "I3@0,S8@10" && uv run eye-sim stats
```
Tests (no LLM), for seed 42 and 2 more seeds:
- Every S8 tuning target in 04 S8.
- The first S8 hour has `orders_hourly` completeness 0.5. The next tick backfills it to 1.0.
- The seller's price on the affected offers changes before any other offer price on these SKUs.
- `sellers.json` has a `manual` entry for the seller at the inject hour, below its unit cost.
- No `change_log` row is written by S8.
- `clear(S8)`: affected offers have their old price; `aov` and `fee_per_order` for electronics return to within ±10% of I3 alone within 3 h of sellout or clear.
- I3 alone matches its oracle: `INNOCENT`, no alert, mentions `coupon-day-electronics` and `planned`. Test coupon expiry after 24 h and clear with no active I3; no coupons outside electronics and no session uplift.
- Keep `sim.start_ts` unchanged; recorded replay fixtures depend on the date.

## Out of scope
SellerOS skills (T17). Agent prompts. The demo runner (T24).
