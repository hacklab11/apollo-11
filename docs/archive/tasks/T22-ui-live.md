# T22 Web UI live wiring

Owner: P3 (coding agent session) + Dev 2 review · Depends on: T18, T21 (T20 for chat) · Blocks: Checkpoint 2 demo

## Goal
The same UI on the real API and SSE stream.

## Read first
`docs/specs/web.md` §1, §3.4.

## Files you own
`web/app.js`, `web/index.html`, `web/styles.css`.

## Steps
1. Live mode is the default; `?mock=1` keeps working.
2. Fetch functions call the real endpoints; `EventSource('/api/events')` with reconnect.
3. Event handling from 13 §3.4.
4. Scenario buttons (S1..S8, I1..I3) call inject/clear; clock controls call pause/resume/step.
5. Error states: API down → banner "core not reachable"; empty incidents → "No open incidents. The system is watching N metrics."

## Done when (checklist in the PR, with screenshots)
- [ ] `./scripts/dev.sh`, open `http://localhost:8000/` → clock ticks.
- [ ] Inject S3 → within 2 ticks an incident appears and its trail grows live.
- [ ] Add metric `fee_per_order` → new node appears.
- [ ] Clear S3 → incident becomes resolved.
- [ ] Mock mode still works.

## Out of scope
Backend changes (report them to Dev 2).
