# T16 Three A2A specialists

Owner: Dev 2 · Depends on: T11, T15 · Blocks: T14 (live), T26

## Goal
Growth, Marketplace, and Discovery & Experience agents, each a real A2A agent with its own card and domain tools.

## Read first
`docs/specs/specialists.md` (all). `docs/specs/tools.md` §4. Official `a2a-sdk` 1.3.1 docs (server side). P3's domain notes (T25).

## Files you own
`src/eye/specialists/app.py`, `growth.py`, `marketplace.py`, `discovery.py`, `tools.py`, `src/eye/llm/prompts/growth.md`, `marketplace.md`, `discovery.md`, `tests/specialists/`, `tests/fixtures/llm/specialist_*.jsonl`. Console script `eye-specialists` (already declared in T00).

## Steps
1. `tools.py`: `httpx` client for `/data/*` as `ToolSpec`s with pydantic I/O and summaries.
2. One agent executor per specialist: parse `SpecialistRequest` from the data part, run `run_agent`, return `SpecialistResponse` as the artifact, plus the step list in metadata (10 §4).
3. Mount 3 A2A apps under one ASGI app on port 8200 with their Agent Cards and skills (10 §2).
4. Prompts with P3's notes.
5. Record fixtures live for the S1 and S7 questions.

## Done when
```bash
uv run pytest tests/specialists -m "not live" -q
uv run eye-specialists &  curl -s localhost:8200/growth/.well-known/agent-card.json | head   # path per SDK
```
Tests: 10 §6 T16 items 1–4.

## Out of scope
SellerOS (T17).
