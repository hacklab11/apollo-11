# T24 Demo runner script and README

Owner: Dev 2 · Depends on: T18 · Blocks: rehearsals

## Goal
A script that plays the demo timeline automatically, and a README that explains the system to judges.

## Read first
`docs/specs/15-demo-and-pitch.md`, `docs/specs/16-deployment.md` §6.

## Files you own
`scripts/demo_run.py`, `README.md`, `docs/assets/` (screenshots).

## Steps
1. `demo_run.py --script demo --base-url http://localhost:8000 [--speed 5]`: calls the public API to inject I1, S7 at +3 h, S6 at +6 h, `add_metric(refund_rate)` at +10 h, clear S7 at +12 h; prints what to say at each step (from 15 §1).
2. `--record` sets `llm.mode=record` through the API (add `POST /api/llm/mode` only if T18 did not; coordinate) so the rehearsal fills the replay cache.
3. README sections from 15 §4, with screenshots from the live UI and the eval table from T23.

## Done when
- `uv run python scripts/demo_run.py --script demo` runs the full timeline against `./scripts/dev.sh` without errors.
- README renders on GitHub; all links work.

## Out of scope
Slides and video (T25).
