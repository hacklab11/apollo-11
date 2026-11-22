# T06 Metric registry and query engine

Owner: Dev 1 · Depends on: T01 (T02/T03 for the metrics on their tables) · Blocks: T07, T08, T15, T18

## Goal
All seed metrics as YAML with the num/den SQL contract, identities, causal seeds, and `query_metric`.

## Read first
`docs/specs/data.md`, `docs/specs/simulator.md` §8, `docs/specs/contracts.md` §2–3.

## Files you own
`config/metrics.yaml`, `config/identities.yaml`, `config/causal_seeds.yaml`, `src/eye/registry/loader.py`, `src/eye/registry/engine.py`, `tests/registry/`.

## Steps
1. Write every metric from 05 §4 (descriptions: use P3's text from T25 if available, else write one sentence and mark `# P3 review`).
2. Loader: parse YAML into `MetricDef`; check names unique; formula expressions reference known metrics; dimensions are valid for the SQL (dry-run 1 hour).
3. Engine: `query_metric`, `metric_value`, `metric_window` (05 §3) with the wrapper SQL and parameter binding (no string formatting of user values). Formula metrics computed in Python from input series. Completeness from `partition_status`. Per-tick cache with `invalidate()`.
4. Record the source tables of each SQL metric (parse `FROM`/`JOIN` names) for `check_freshness`.
5. Metrics whose tables are not implemented yet: loader marks them `pending` and skips them, with a warning.

## Done when
```bash
uv run pytest tests/registry -m "not live" -q
```
Tests: 05 §7 items 1–5 on a 7-day world fixture.

## Out of scope
Anomaly detection, graph.
