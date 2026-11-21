# T01 Simulator: world, demand, checkout funnel, store, CLI

Owner: Dev 1 · Depends on: T00 · Blocks: T02, T03, T06

## Goal
A seeded simulator that builds the static world and generates traffic, search, feed, and the checkout funnel hour by hour into DuckDB, with a CLI to generate, export, and show stats.

## Read first
`docs/specs/03-simulator.md` §1, §2, §3, §8, §10. `docs/specs/01-architecture.md` §2.

## Files you own
`src/eye/sim/world.py`, `src/eye/sim/demand.py`, `src/eye/sim/engine.py`, `src/eye/sim/store.py`, `src/eye/sim/cli.py`, `tests/sim/test_world.py`, `tests/sim/test_demand.py`, `tests/sim/conftest.py`.

## Do not touch
`src/eye/contracts.py`, other `sim/` modules (T02/T03 own them; create them as stubs with a `TODO(T02)` only if you need an import).

## Steps
1. `store.py`: create all tables from 03 §8 (all of them, so later tasks only insert). One write connection with a lock; `cursor()` for readers. Batch insert per table per hour.
2. `world.py`: SKUs, sellers (incl. dormant S-99), offers (3–5 per SKU), suppliers, advertisers, `app_components`. Deterministic from `sim.seed`.
3. `demand.py`: sessions (03 §3.1), search (§3.2), feed (§3.3), conversion through the checkout funnel (§3.4) up to `payment success`. For now, assign orders to the cheapest offer (T02 replaces this with the Buy Box). Write `traffic_hourly`, `search_hourly`, `feed_hourly`, `checkout_hourly`, `payments_hourly` (PSP split), and `orders_hourly` with gmv only.
4. `engine.py`: `Simulator(settings)`, `backfill()`, `step()`, `now`. Per-hour RNG = `Generator(seed + hour_index)`. Hooks list for later modules: `pre_demand_hooks`, `post_demand_hooks` (T02/T03 register into them).
5. `cli.py` → console script `eye-sim`: `generate --days --seed [--play "ID@h,..."]` (play: parse and store, call `scenarios.inject` if the module exists, else error "scenarios not implemented"), `export --format parquet|csv --out`, `stats`.
6. Vectorize with numpy per hour; aim for 28 days backfill < 60 s.

## Done when
```bash
uv run pytest tests/sim -m "not live" -q
uv run eye-sim generate --days 28 --seed 42 && uv run eye-sim stats
```
Tests cover 03 §9 items 1, 3, 5, 6, 8 (orders = payment successes per country/platform).

## Out of scope
Buy Box, repricers, stock, finance, ads, errors, scenarios.
