# T20 Chat via MCP

Owner: Dev 2 · Depends on: T11, T19, T18 · Blocks: T22 (chat panel)

## Goal
`POST /api/chat` answers questions about the system by calling our own MCP server, proving that any agent can use it.

## Read first
`docs/specs/web.md` §2. `docs/specs/agent-runtime.md`. Official `mcp` 2.3.0 client docs.

## Files you own
`src/eye/api/chat.py`, `src/eye/llm/prompts/chat.md`, `tests/api/test_chat.py`.

## Steps
1. MCP client session (streamable HTTP) opened per request (or pooled with reconnect).
2. Convert MCP tools to `ToolSpec`s whose `fn` calls `session.call_tool`.
3. Run the `chat` agent (effort `medium`, max steps 8); return `{answer, tool_calls, incident_ids}`.
4. Input limits (20 messages, 4 000 chars each); no other tools.
5. Router mounted in `api/app.py` (one line; coordinate with T18).

## Done when
```bash
uv run pytest tests/api/test_chat.py -m "not live" -q
```
Test: 13 §4 T20 (fake LLM calls `list_incidents`; the MCP tool runs; the answer contains the incident id).

## Out of scope
UI.
