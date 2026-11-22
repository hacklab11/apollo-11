# T13 Sentinel agent

Owner: Dev 2 · Depends on: T07, T11, T12 (T09 optional) · Blocks: T18

## Goal
Each tick, decide what to investigate, watch, defer, ignore, or attach, with reasons.

## Read first
`docs/specs/09-core-agents.md` §1, §2, §7 (T13). `docs/specs/07-tools.md` §3.1–3.2.

## Files you own
`src/eye/agents/sentinel.py`, `src/eye/llm/prompts/sentinel.md`, `tests/agents/test_sentinel.py`, `tests/fixtures/llm/sentinel_*.jsonl`.

## Steps
1. Prefetch (code): scans, freshness, events (24 h), open incidents → compact user message (max ~6 000 tokens; trim lowest priority first).
2. Agent spec: tools `baseline_compare`, `check_freshness`, `list_events`, `get_graph`; submit `list[SentinelDecision]`; effort `low`; max steps 4.
3. Code rules after submit: force `defer` on incomplete data; drop decisions for anomalies that were not in the input; keep `watch` state between ticks (two ticks → `investigate`).
4. No candidates above threshold → skip the LLM call (cost).
5. Prompt with the decision rules of 09 §2 and P3's domain notes.

## Done when
```bash
uv run pytest tests/agents/test_sentinel.py -m "not live" -q
```
Tests: 09 §7 T13 list; no LLM call when there are no candidates.

## Out of scope
Starting investigations (T18 does it).
