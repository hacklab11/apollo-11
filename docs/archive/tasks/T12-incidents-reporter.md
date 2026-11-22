# T12 Incident store and Reporter

Owner: Dev 2 · Depends on: T00 · Blocks: T13, T18

## Goal
Incidents with fingerprints, matching, merging, lifecycle, trail storage, and the Reporter's narrative.

## Read first
`docs/specs/core-agents.md` §5, §7 (T12). `docs/specs/contracts.md` §5–6.

## Files you own
`src/eye/incidents/store.py`, `src/eye/agents/reporter.py`, `src/eye/llm/prompts/reporter.md`, `tests/incidents/`.

## Steps
1. Storage: DuckDB tables `incidents` (JSON column per incident) and `trail_steps` in a separate file `data/ops.duckdb` (keeps ops data apart from the world; one writer: core).
2. `canonical(segment)`, `fingerprint()`, `match(decision, graph)` (09 §5.2), `upsert(result)` with merge and related links (09 §5.3), `lifecycle(ts, z_lookup)` (09 §5.4), reopen rule.
3. `Reporter.write(result) -> (title, summary)` with the LLM (structured output; code passes the numbers; reject output that changes a number: compare numbers in text against the result, re-ask once, then fall back to a template).
4. SellerOS notification call through an injected `notify` function (T15 provides the real one).
5. Graph dependency: accept a `path_exists(src, dst)` callable, so this card does not wait for T09.

## Done when
```bash
uv run pytest tests/incidents -m "not live" -q
```
Tests: 09 §7 T12 list; Reporter fallback template when the fake LLM returns a wrong number.

## Out of scope
Investigation logic.
