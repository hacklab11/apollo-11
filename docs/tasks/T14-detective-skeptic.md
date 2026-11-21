# T14 Lead Detective and Skeptic

Owner: Dev 2 · Depends on: T07, T08, T09, T11, T15 (T16 for live runs) · Blocks: Checkpoint 2

## Goal
Autonomous investigation: hypothesis board, graph walk, specialists over A2A, Skeptic checks, loop handling, code-checked stop rules.

## Read first
`docs/specs/09-core-agents.md` §1, §3, §4, §7 (T14). `docs/specs/07-tools.md` §2–3. `docs/specs/10-a2a-and-specialists.md` §3. `docs/specs/04-scenarios.md`.

## Files you own
`src/eye/agents/detective.py`, `src/eye/agents/skeptic.py`, `src/eye/llm/prompts/detective.md`, `src/eye/llm/prompts/skeptic.md`, `tests/agents/test_detective.py`, `tests/agents/test_skeptic.py`, `tests/fixtures/llm/detective_*.jsonl`, `tests/fixtures/llm/skeptic_*.jsonl`.

## Steps
1. `investigate(anomaly, on_step) -> InvestigationResult`. Fixed windows: current = [anomaly.ts − window+1h, anomaly.ts + 1h); baseline from the tools' default.
2. Tool set from 09 §3.2. `ask_specialist` wraps `A2AClient.ask`; `ask_skeptic` runs the Skeptic agent; `update_hypotheses` stores the board.
3. Prompt: method 9 steps (09 §3.3), loop rule (§3.4), how evidence groups work, example trail (§3.6) as a format example only.
4. Code stop-rule checks on submit (09 §3.5): downgrade with reasons; residual row for contributions.
5. Loop detection helper: `graph.cycles_through` + `first_change` → fills `loop` and `trigger` if the LLM left them empty but the data shows a cycle of changed nodes.
6. Skeptic agent (09 §4).
7. Record fixtures live for S3 and S7 (needs T16 running); later S1, S2, S4, S5, S6 when their data exists.

## Done when
```bash
uv run pytest tests/agents/test_detective.py tests/agents/test_skeptic.py -m "not live" -q
uv run python scripts/eval_scenarios.py --scenario S3 --mode live     # after T23; before that: tests/agents/test_detective.py -m live
```
Tests: 09 §7 T14 list; weak `CONFIRMED` (one evidence group) is downgraded; A2A unavailable → trail note, investigation continues.

## Out of scope
Specialist internals, orchestration.
