# T18 Orchestrator, public API, SSE, dev scripts

Owner: Dev 2 · Depends on: T06, T09, T12, T13 (T14 for full flow) · Blocks: T20, T22, T23, T24, T26

## Goal
The running system: tick loop, investigations in parallel, public API, live events, and one command to start everything.

## Read first
`docs/specs/01-architecture.md` §1, §3, §5, §7. `docs/specs/09-core-agents.md` §6. `docs/specs/13-api-and-ui.md` §1, §4 (T18).

## Files you own
`src/eye/orchestrator.py`, `src/eye/api/app.py`, `src/eye/api/events.py`, `src/eye/main.py` (`eye-core`), `scripts/dev.sh`, `scripts/reset.sh`, `tests/api/test_public_api.py`, `tests/test_orchestrator.py`.

## Steps
1. Startup: load settings, open DuckDB, backfill if empty, load/bootstrap graph (and call `refine_causal_seeds` once if T10 is merged), A2A cards fetch, start clock if `sim.autostart`.
2. Tick loop exactly as 01 §3, with `max_concurrent_investigations`, `pause_sim_during_investigation`, global LLM budget.
3. Event bus → SSE `/api/events` (all event types in 02 §7); heartbeat comment every 15 s.
4. All endpoints in 13 §1 (including clock controls, scenarios, add metric → T10, demo truth behind config).
5. Step mode for tests and the eval harness: `Orchestrator.step_once()` without the timer.
6. `scripts/dev.sh`: start core, specialists, selleros, mcp with prefixed logs; stop all on Ctrl+C. `scripts/reset.sh`: delete `data/` and SellerOS data.

## Done when
```bash
uv run pytest tests/api/test_public_api.py tests/test_orchestrator.py -m "not live" -q
./scripts/dev.sh   # by hand: curl localhost:8000/api/status shows all agents ok; SSE ticks arrive
```
Tests: 13 §4 T18; step mode with fake agents: a candidate → investigation → incident upserted → lifecycle resolves after clear.

## Out of scope
UI code, chat.
