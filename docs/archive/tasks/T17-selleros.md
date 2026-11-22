# T17 SellerOS external agent

Owner: Dev 2 · Depends on: T02 (sellers.json), T11 (runtime to copy) · Blocks: S6, S8 live demo, T26

## Goal
A separate project that acts as another company's agent: answers seller-side questions over A2A and acts on alerts.

## Read first
`docs/specs/selleros.md`, `docs/specs/specialists.md` §1, `docs/specs/contracts.md` §8.

## Files you own
Everything under `external/selleros/` (own `pyproject.toml`, package `selleros`, prompts, tests), except `external/selleros/data/` (generated).

## Steps
1. New uv project, Python 3.13, deps: `anthropic`, `a2a-sdk`, `pydantic`, `uvicorn`. Console script `selleros`.
2. Copy the runtime from `src/eye/llm/runtime.py` + `client.py` into `selleros/runtime.py` (no import of `eye`). Copy the needed pydantic models (A2A payloads) into `selleros/models.py`.
3. Data access over `data/sellers.json` (re-read per request); actions log `data/actions.jsonl`.
4. A2A server on 8300 with the 5 skills (11 §4) and the privacy rule. `price_intent_check` is a deterministic rule (11 §4); the LLM only words the answer.
5. Alert handling for "revert manual price" (11 §4).
6. Tests including the isolation test.

## Done when
```bash
cd external/selleros && uv sync && uv run pytest -m "not live" -q
uv run selleros &  # card reachable on :8300
grep -rE "^(from|import) eye" external/selleros && exit 1 || true
```
Tests: 11 §6 items 1–7.

## Out of scope
Changing the simulator.
