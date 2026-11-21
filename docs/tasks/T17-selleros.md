# T17 SellerOS external agent

Owner: Dev 2 · Depends on: T02 (sellers.json), T11 (runtime to copy) · Blocks: S6 demo, T26

## Goal
A separate project that acts as another company's agent: answers seller-side questions over A2A and acts on alerts.

## Read first
`docs/specs/11-selleros-external.md`, `docs/specs/10-a2a-and-specialists.md` §1, `docs/specs/02-contracts.md` §8.

## Files you own
Everything under `external/selleros/` (own `pyproject.toml`, package `selleros`, prompts, tests), except `external/selleros/data/` (generated).

## Steps
1. New uv project, Python 3.13, deps: `anthropic`, `a2a-sdk`, `pydantic`, `uvicorn`. Console script `selleros`.
2. Copy the runtime from `src/eye/llm/runtime.py` + `client.py` into `selleros/runtime.py` (no import of `eye`). Copy the needed pydantic models (A2A payloads) into `selleros/models.py`.
3. Data access over `data/sellers.json` (re-read per request); actions log `data/actions.jsonl`.
4. A2A server on 8300 with the 4 skills (11 §4) and the privacy rule.
5. Tests including the isolation test.

## Done when
```bash
cd external/selleros && uv sync && uv run pytest -m "not live" -q
uv run selleros &  # card reachable on :8300
grep -rE "^(from|import) eye" external/selleros && exit 1 || true
```
Tests: 11 §6 items 1–5.

## Out of scope
Changing the simulator.
