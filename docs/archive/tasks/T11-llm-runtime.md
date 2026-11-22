# T11 LLM agent runtime with record/replay

Owner: Dev 2 · Depends on: T00 · Blocks: T10, T13, T14, T16, T17, T20

## Goal
One reusable, tested agent loop on the Claude Messages API with tool calling, trail steps, budgets, and record/replay.

## Read first
`docs/specs/agent-runtime.md`, `docs/specs/architecture.md` §4.1, the official `anthropic` Python SDK docs for version 2.12.0 (messages, tool use, errors, prompt caching, structured outputs).

## Files you own
`src/eye/llm/runtime.py`, `src/eye/llm/client.py` (thin wrapper: live/record/replay), `src/eye/llm/prompts/README.md`, `tests/llm/`.

## Steps
1. `ToolSpec`, `AgentSpec`, `run_agent` exactly as 08 §1.
2. Loop from 08 §2, with `submit` as a strict tool built from `submit_model`.
3. Client wrapper: model, effort, fallbacks beta, cache_control on system, retries; record/replay keyed as 08 §3; fake transport for tests.
4. Budget counters (08 §4) exposed for `/api/status`.
5. TrailStep emission through `on_step`.

## Done when
```bash
uv run pytest tests/llm -m "not live" -q
uv run pytest tests/llm -m live -q      # by hand: one real 2-tool round trip with claude-opus-5-5
```
Tests: 08 §6 items 1–6.

## Out of scope
Any concrete agent prompt except a test agent.
