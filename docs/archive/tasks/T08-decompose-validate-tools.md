# T08 Decompose, mix/rate split, validation tools

Owner: Dev 1 · Depends on: T06 · Blocks: T09, T14

## Goal
Exact contribution math along formula edges and the validators the Cartographer uses.

## Read first
`docs/specs/tools.md` §3.3, §3.4. `docs/specs/cartographer.md` §3. `docs/specs/data.md` §5.

## Files you own
`src/eye/tools/drill.py` (add `decompose`, `mix_rate_split` only), `src/eye/tools/validate.py`, `src/eye/tools/identity.py` (parser for identity strings), `tests/tools/test_decompose.py`, `tests/tools/test_validate.py`.

## Steps
1. Identity parser: `"y = a * b / c"` and sums with `+ -`; return target, terms, exponents/signs. Reject anything else with a clear error.
2. `decompose` (sum and product/quotient with log attribution, residual row).
3. `mix_rate_split` (exact shift-share).
4. `validate_identity`, `validate_breakdown`, `lagged_correlation` (de-seasonalized first differences), with evidence dicts.
5. Exclude hours listed as "scenario hours" when the caller passes them.

## Done when
```bash
uv run pytest tests/tools -m "not live" -q
```
Tests: 07 §5 items 3, 4; 05 §7 item 6 (all identities F1–F8 residual < 0.5% on baseline); `lagged_correlation` finds lag 24 ± 2 on a synthetic series shifted by 24 h.

## Out of scope
Graph storage.
