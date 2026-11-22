# T10 Cartographer agent and add_metric

Owner: Dev 1 · Depends on: T09, T11 · Blocks: demo step "add metric"

## Goal
An agent that places a new metric in the graph from its name, description, and SQL only, validates the edges, and handles ambiguity; plus LLM refinement of causal seeds at bootstrap.

## Read first
`docs/specs/cartographer.md` §2 (LLM part), §5, §6, §7 (T10 tests). `docs/specs/agent-runtime.md`. `docs/specs/architecture.md` §6 (SQL guard).

## Files you own
`src/eye/agents/cartographer.py`, `src/eye/tools/sqlguard.py`, `src/eye/llm/prompts/cartographer.md`, `tests/agents/test_cartographer.py`, `tests/fixtures/llm/cartographer_*.jsonl`.

## Steps
1. `sqlguard.check(sql, allowed_tables)`: parse with DuckDB (`json_serialize_sql` or the parser API), one SELECT only, deny list from 01 §6. Run on a read-only cursor with a LIMIT wrapper.
2. `add_metric(req) -> AddMetricResult`: steps 1–7 of 06 §5, each writing a TrailStep.
3. Bootstrap refinement: `refine_causal_seeds()` (06 §2 step 5), called once by T18 when `graph.json` has unrefined seeds.
4. Prompt file from the outline in 06 §5 + P3's notes.
5. Record fixtures with one live run (`-m live`, by hand), then make tests replay them.

## Done when
```bash
uv run pytest tests/agents/test_cartographer.py -m "not live" -q
uv run pytest tests/agents/test_cartographer.py -m live -q     # by hand once, records fixtures
```
Tests: 06 §7 T10 items 1–4.

## Out of scope
API endpoint (T18 calls `add_metric`), UI modal (T21).
