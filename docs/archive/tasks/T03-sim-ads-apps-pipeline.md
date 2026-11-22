# T03 Simulator: ads, marketing, payments, client errors, change log, pipeline

Owner: Dev 1 · Depends on: T01 · Blocks: T04

## Goal
The engineering and growth data sources: ad auction with pacing, marketing spend, PSP behaviour, client errors by app version, the change log with baseline noise, and partition status.

## Read first
`docs/specs/simulator.md` §3.5, §5, §7, §8 (tables `ads_hourly`, `marketing_hourly`, `payments_hourly`, `client_errors_hourly`, `app_components`, `external_status`, `change_log`, `partition_status`).

## Files you own
`src/eye/sim/ads.py`, `src/eye/sim/apps.py`, `src/eye/sim/pipeline.py`, `src/eye/sim/changelog.py`, `tests/sim/test_ads.py`, `tests/sim/test_apps_pipeline.py`.

## Steps
1. `ads.py`: 8 advertisers, budgets, bids, second-price auction per category per hour, pacing controller with `planned_cum_share` and version flag `pacing-svc v4.0`, bid escalation from T02's out-of-stock hours (if T02 is not merged yet, read a zero map). Ad clicks add to the advertiser's SKU clicks.
2. Marketing: `marketing_hourly` with channels and `marketing_mult` hook for scenarios.
3. `apps.py`: app versions per platform (web/checkout-web switch at release; android/ios 20%/day rollout), client errors baseline, `checkout_ok[platform]` and `psp_success[psp]` and `psp_latency[psp]` as scenario hooks used by T01's funnel.
4. `changelog.py`: baseline noise releases (~1/day, systems from 03 §8), API `add_change(...)`, `add_external_status(...)` for scenarios.
5. `pipeline.py`: `partition_status` per table per country per hour; hook for late/incomplete data (I2).

## Done when
```bash
uv run pytest tests/sim -m "not live" -q
uv run eye-sim generate --days 28 --seed 42 && uv run eye-sim stats
```
Tests: baseline `budget_pace_ratio` within 0.8–1.2 at 12:00 for all advertisers; spend never exceeds budget; client errors ≈ sessions × 0.002 (±20%); partition completeness = 1.0 in baseline; ~28 noise releases in 28 days.

## Out of scope
Scenario injections (T04).
