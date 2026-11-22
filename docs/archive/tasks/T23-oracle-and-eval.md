# T23 Scenario oracle (P3) and eval harness (Dev 1)

Depends on: T04 (data), T27 (S8/I3 data), T18 (step mode) · Blocks: Checkpoint 3

## Goal
A machine-checkable definition of "the system got it right" for every scenario, and a script that runs it.

## Read first
`docs/specs/04-scenarios.md`, `docs/specs/14-testing-and-evals.md`.

## Part A — P3: `tests/oracle/scenarios.yaml`
1. One entry per scenario S1–S8, I1–I3 and per combined run D1–D6, in the format of 14 §2. D6 is the live demo run: write it first (`I3@0,S8@10`). Include an I3-alone entry: `INNOCENT`, no alert; mention `coupon-day-electronics` and `planned`.
2. Take every `must_mention`, `must_not_claim`, verdict, root node, and segment from 04. If 04 is unclear, ask Dev 1 and fix 04 first.
3. Keep words that a correct report will surely contain (ids, version numbers, segment names). Avoid style words.

## Part B — Dev 1: `scripts/eval_scenarios.py`
Files you own: `scripts/eval_scenarios.py`, `tests/test_eval_harness.py`.
1. Steps in 14 §3; options `--scenario ID | --all`, `--mode live|replay`, `--record PATH`, `--record-dir DIR`, `--seed`.
2. Output table: scenario, each check pass/fail, ticks to detect, LLM calls, tokens, seconds. JSON in `data/eval/`.
3. Exit code 0 only if all checks pass.
4. Unit test of the checker with a hand-made incident (no LLM).

## Done when
```bash
uv run pytest tests/test_eval_harness.py -q
uv run python scripts/eval_scenarios.py --scenario S3 --mode live
```

## Out of scope
Fixing agents (that is T13/T14 work after the eval shows a failure).
