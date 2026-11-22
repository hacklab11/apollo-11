# T15 Data API and A2A client

Owner: Dev 2 · Depends on: T06 (tables from T01–T03) · Blocks: T14, T16

## Goal
Read-only domain endpoints for the specialists, and the core-side A2A client with health checks and safe failures.

## Read first
`docs/specs/tools.md` §4, `docs/specs/specialists.md` §1, §3, §5, §6 (T15). Official `a2a-sdk` 1.3.1 docs (client side).

## Files you own
`src/eye/api/data_api.py`, `src/eye/a2a_client.py`, `tests/api/test_data_api.py`, `tests/a2a/test_client.py`, `tests/a2a/stub_server.py`.

## Steps
1. FastAPI `APIRouter` at `/data` with every endpoint in 07 §4; param validation; row limit; `{rows, summary}`.
2. `A2AClient.fetch_cards`, `ask`, `notify_seller_os` (10 §3), using the SDK client. Validate payloads with pydantic.
3. Stub A2A server for tests built with the SDK (one skill that echoes a fixed `SpecialistResponse`).
4. If the 1.3.1 client API blocks you for more than 1 hour: implement `message/send` JSON-RPC with `httpx` against the A2A spec and keep the same interface (plan risk table).

## Done when
```bash
uv run pytest tests/api/test_data_api.py tests/a2a -m "not live" -q
```
Tests: 10 §6 T15 items 1–2.

## Out of scope
Specialist agents (T16).
