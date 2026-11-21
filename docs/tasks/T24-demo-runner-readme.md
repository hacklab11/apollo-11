# T24 Demo runner script and README

Owner: Dev 2 · Depends on: T18, T27, T17 · Blocks: rehearsals

## Goal
A script that plays the demo timeline automatically, and a README that explains the system to judges.

## Read first
`docs/specs/15-demo-and-pitch.md`, `docs/specs/16-deployment.md` §6.

## Files you own
`scripts/demo_run.py`, `README.md`, `docs/assets/` (screenshots).

## Steps
1. `demo_run.py --script demo --base-url http://localhost:8000 [--speed 5]`: plays run D6 (04) through the public API: start at 09:00 on a normal day with I3 active since 00:00, inject S8 at 10:00, `add_metric(fee_per_order)` when the S8 incident exists, `clear(S8)` when the incident shows the SellerOS ack, then wait for `resolved` after 3 normal ticks (about 15 s at 5 s per sim hour; check in rehearsal). Do not change `sim.start_ts`; recorded replay fixtures depend on its date. Wait on incident state, not on fixed times. Print what to say at each step (from 15 §1.2).
2. `--record` sets `llm.mode=record` through the API (add `POST /api/llm/mode` only if T18 did not; coordinate) so the rehearsal fills the replay cache.
3. README sections from 15 §4, with screenshots from the live UI and the eval table from T23.

## Done when
- `uv run python scripts/demo_run.py --script demo` runs the full D6 timeline against `./scripts/dev.sh` without errors, and prints the wall time of each step (the target is 3 minutes in total).
- README renders on GitHub; all links work.

## Out of scope
Slides and video (T25).
