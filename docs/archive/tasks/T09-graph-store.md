# T09 Graph store, bootstrap, impact weights

Owner: Dev 1 · Depends on: T08 · Blocks: T10, T13, T14, T18

## Goal
The typed metric graph, built deterministically from the registry, persisted, with cycle detection and impact weights.

## Read first
`docs/specs/cartographer.md` §1–4, §7 (T09 tests). `docs/specs/contracts.md` §2.

## Files you own
`src/eye/graph/store.py`, `src/eye/graph/bootstrap.py`, `tests/graph/`.

## Steps
1. `GraphStore` API from 06 §1, including `view()` for the UI (nodes with state and impact weight).
2. Deterministic bootstrap steps 1–4 (formula edges from identities, breakdown edges, impact weights). Causal seeds are loaded as `hypothesis` with confidence 0.5 and `evidence = lagged_correlation` result; T10 refines them with the LLM.
3. Persist `data/graph.json` with `registry_hash`.
4. Event hook `on_change(callback)` (T18 connects it to SSE).
5. Replace T07's default impact weights with the graph's (one function `impact_weight(metric)`).

## Done when
```bash
uv run pytest tests/graph -m "not live" -q
```
Tests: 06 §7 T09 items 1–5.

## Out of scope
LLM refinement of causal edges, add_metric (T10).
