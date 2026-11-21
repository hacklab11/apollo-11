# T27 Scenario S8: price typo (live demo)

Owner: Dev 1 · Depends on: T04 (scenario manager, I1, I2 late-partition hook), T02 (`sellers.json`) · Blocks: T23 (D6), T24, rehearsals

## Goal
Inject and clear S8 "The €9 headphones" so the data matches the scenario bible. This is the scenario of the 3-minute live demo.

## Read first
`docs/specs/04-scenarios.md` S8, I1, I2 and run D6; `docs/specs/15-demo-and-pitch.md` §1; `docs/specs/03-simulator.md` §3.4, §4.2, §7; `docs/specs/11-selleros-external.md` §3.

## Before you start
`ScenarioId` in `src/eye/contracts.py` must contain `"S8"` (`docs/specs/02-contracts.md` §10). Per `docs/plan/implementation-plan.md` §2 item 5, a developer makes this change by hand on `main`. If `"S8"` is missing, stop and report it. Do not change `contracts.py` in this PR.

## Files you own
`src/eye/sim/scenarios.py` (S8 parts only), `config/scenarios.yaml` (S8 section), `tests/sim/test_scenario_s8.py`. The code that writes `external/selleros/data/sellers.json` may get the `source` field (list the file in the PR).

## Steps
1. Choose `scenarios.S8.seller_id` for seed 42 by the rule in 04 S8. Write it in `config/scenarios.yaml` with a comment.
2. `inject(S8)`: price × 0.10 on the seller's offers on E01–E10, repricer mode `fixed` on these offers, stock for a 4–8 h sellout, and the I2 late-partition hook for the first S8 hour (both countries, share 0.6, backfill at the next tick). No `change_log` row.
3. Write the price change to the seller's `price_history` with `source: "manual"`. Other entries get `source: "repricer"` (or no `source`).
4. `clear(S8)`: restore the pre-S8 price and repricer mode on the affected offers. Sold stock stays sold.
5. Use the hooks from T01–T04 only. No special cases inside demand or market code.

## Done when
```bash
uv run pytest tests/sim/test_scenario_s8.py -m "not live" -q
uv run eye-sim generate --days 28 --seed 42 --play "I1@0,S8@10" && uv run eye-sim stats
```
Tests (no LLM), for seed 42 and 2 more seeds:
- Every S8 tuning target in 04 S8.
- The first S8 hour has `orders_hourly` completeness 0.6. The next tick backfills it to 1.0.
- The seller's price on the affected offers changes before any other offer price on these SKUs.
- `sellers.json` has a `manual` entry for the seller at the inject hour, below its unit cost.
- No `change_log` row is written by S8.
- `clear(S8)`: affected offers have their old price; `aov` and `fee_per_order` for electronics return to within ±10% of I1 alone within 3 h of sellout or clear.
- I1 alone still passes its T04 test.

## Out of scope
SellerOS skills (T17). Agent prompts. The demo runner (T24).
