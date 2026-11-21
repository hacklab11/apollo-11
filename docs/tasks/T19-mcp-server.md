# T19 MCP server

Owner: Dev 2 · Depends on: T00 (API contract); T18 for the live test · Blocks: T20, T26

## Goal
Any MCP client can read incidents, the graph, metrics, add a metric, and drive the demo scenarios.

## Read first
`docs/specs/12-mcp-server.md`, `docs/specs/13-api-and-ui.md` §1. Official `mcp` 2.3.0 Python SDK docs (server, streamable HTTP).

## Files you own
`src/eye/mcp_server.py`, `tests/mcp/`.

## Steps
1. Server with the 10 tools in 12 §2; each calls the core API with `httpx` (base URL from config).
2. Streamable HTTP on `127.0.0.1:8100/mcp`; console script `eye-mcp`.
3. Errors as in 12 §4.
4. Tests against a fake core API (`httpx.MockTransport` or a tiny FastAPI app serving `web/mock/*.json`).
5. Manual check with Claude Code (12 §5 item 4) once T18 is merged; paste the transcript summary in the PR.

## Done when
```bash
uv run pytest tests/mcp -m "not live" -q
```
Tests: 12 §5 items 1–3.

## Out of scope
Resources (optional, only if time remains), chat.
