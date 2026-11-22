# AGENTS.md — Eye of the System

Instructions for coding agents (Devin, Claude Code, others). Humans: see `README.md` and `docs/README.md`.

## Project

A fully agentic monitoring system for a simulated marketplace (hackathon, 24 hours).
Simulator → DuckDB → metric graph → Sentinel / Detective / Skeptic / Reporter agents → incidents.
Three specialist agents and one external agent (SellerOS) over A2A. An MCP server. A one-page web UI.

## Read before you code

1. `docs/README.md` (index)
2. Your task card in `docs/tasks/`
3. The specs your card lists under "Read first"
4. `docs/specs/02-contracts.md` — all shared data shapes. New code uses `packages/contracts/src/eye/contracts_v2.py`; v1 remains in `contracts.py`. Do not redefine models.

## Rules

- Do only your task card. Change only the files under "Files you own". If you must change another file, explain why in the PR.
- Do not change `packages/contracts/src/eye/contracts.py`, `pyproject.toml`, or `config/settings.yaml` unless your card is T00 or the card says so.
- If the spec is unclear: choose the reversible option, write it under "Assumptions" in the PR, continue. Do not stop to ask.
- If the spec is wrong (it contradicts itself or the data): write it in the PR under "Spec issues". Do not silently do something else.
- Smallest correct change. No extra features, no speculative config, no new dependencies unless the card allows it.
- Tools compute, agents decide: numbers come from deterministic code, never from LLM text.
- Never put secrets in code, tests, fixtures, logs, or commits. `ANTHROPIC_API_KEY` comes from the environment.
- Never commit `data/`, `.env`, or `packages/selleros/data/`.
- No agent tool may read the `scenario_log` table (ground truth).
- Bind servers to `127.0.0.1`.

Production cannot import `eye.sim`; contracts/config are foundational; data/LLM stay below agents and orchestration; agents/specialists stay below orchestration. SellerOS is independent; `packages/agent_runtime` imports neither Eye nor SellerOS and its scripted-model tests run in the root suite. Import-linter documents temporary wave 3 exceptions.

## Stack

- Python 3.13 with `uv`. Do not use the system `python3` (3.9).
- `anthropic` 2.12.0, `a2a-sdk` 1.3.1, `mcp` 2.3.0, `duckdb` 2.0.1, `fastapi` 0.165.0, `pydantic` v2, `networkx`, `numpy`, `httpx`, `pyyaml`.
- For `anthropic`, `a2a-sdk`, and `mcp`: read the official docs of the installed version before you write code. Do not guess class or method names. If a name fails, look it up; do not try variants blindly.
- Claude API rules: `docs/specs/01-architecture.md` §4.1 (model `claude-opus-5-5`; no disabled thinking; effort via `output_config`; `tool_choice` auto; check `stop_reason`; all tool results in one user message).

## Commands

```bash
uv sync                                   # install
uv run ruff check . && uv run ruff format --check .
uv run lint-imports
uv run pytest -m "not live" -q            # must pass before every PR
uv run eye-sim generate --days 28 --seed 42
./scripts/dev.sh                          # run all processes locally
```

Tests marked `live` call the real API. Do not run them in CI. Run them only when your card says so.

## Code style

- Type hints everywhere. Pydantic models at every boundary (API, A2A, MCP, tools).
- Every tool output model has `summary: str` (≤ 300 chars, with numbers).
- Async for I/O (HTTP, LLM). Sync is fine for DuckDB and numpy.
- Comments only for the reason behind non-obvious code.
- Logs: JSON lines to stdout, no prompts, no secrets.

## Pull requests

- Branch: `task/<card-id>-<slug>`, for example `task/T07-stats-tools`.
- Title: `<card-id>: <short title>`.
- Description sections: Summary, Files changed, Done-when evidence (paste command output), Assumptions, Spec issues, Not done.
- One card per PR.
