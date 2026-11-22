# T24 Demo runner script and README

Historical card, superseded by W2-7. The old timeline runner and reset utility
have been removed. Current commands against an already-running continuous stack:
`uv run demo trigger s8`, `uv run demo status`, `uv run demo narrate`.
The steps below describe the original design, not current run instructions.

Owner: Dev 2 · Depends on: T18, T27, T17 · Blocks: rehearsals

## Goal
A script that plays the demo timeline automatically, and a README that explains the system to judges.

## Read first
`docs/specs/demo.md`, `docs/specs/deployment.md` §6.

## Files you own
Legacy runner (removed), `README.md`, `docs/assets/` (screenshots).

## Steps
1. The legacy runner played run D6 (04) through the public API with a prepared clock and coupon pre-state, metric registration, and recovery checks. This is no longer a supported command.
2. `--record` sets `llm.mode=record` through the API (add `POST /api/llm/mode` only if T18 did not; coordinate) so the rehearsal fills the replay cache.
3. README sections from 15 §4, with screenshots from the live UI and the eval table from T23.

## Done when
- Historical acceptance: the legacy runner played the full D6 timeline and printed wall time (target: 3 minutes).
- README renders on GitHub; all links work.

## Out of scope
Slides and video (T25).
