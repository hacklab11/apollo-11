# T07 Baseline, scans, drill-down, context tools

Owner: Dev 1 · Depends on: T06 · Blocks: T13, T14

## Goal
The deterministic tools that the Sentinel and the Detective use to measure, scan, locate, and get context.

## Read first
`docs/specs/07-tools.md` §1, §2, §3.1, §3.2, §3.5–3.8, §5. `docs/specs/02-contracts.md` §4.

## Files you own
`src/eye/tools/stats.py`, `src/eye/tools/drill.py` (drill_down, compare_segments, first_change only), `src/eye/tools/context.py`, `src/eye/tools/models.py` (input/output models with `summary`), `tests/tools/test_stats.py`, `tests/tools/test_context.py`.

## Steps
1. `expected()` baseline with fallback and z floor (07 §1).
2. `baseline_compare`, `scan_anomalies`, `scan_divergent_segments` (07 §3.1–3.2), using impact weights from the graph if available, else 1.0 for `contribution_margin`/`gmv`/`orders` and 0.3 for others (T09 replaces it).
3. `drill_down`, `compare_segments`, `first_change`.
4. `check_freshness`, `list_events` (change_log + external_status; never scenario_log), `planned_effect_check`.
5. Every output model has `summary` with key numbers.
6. Performance: `scan_anomalies` over all metrics × segments for one hour in < 3 s.

## Done when
```bash
uv run pytest tests/tools -m "not live" -q
```
Tests: 07 §5 items 1, 2, 5, 6, 7, 8 (use T04 scenarios; S6 test may be skipped until T05 is merged, mark `skipif`).

## Out of scope
Decompose and validation (T08).
