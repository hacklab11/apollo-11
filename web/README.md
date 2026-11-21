# Live observatory

Plain HTML, CSS and ES modules served by the core API at `http://127.0.0.1:8000/`.
No install or build step. Cytoscape.js 3.30.4 and Chart.js 4.4.8 load from
pinned jsDelivr URLs; Google Fonts is optional (system sans-serif is the fallback).
Internet access is needed for the charts.

The exported `api` object in `app.js` calls the core's `/api` endpoints.
The activity feed uses `/api/events` via SSE. Clock, scenario and metric controls
operate on the backend; there is no offline fixture mode.

Chat posts `{messages: [{role, content}]}` to `/api/chat` (up to 20 messages,
4000 characters each). It renders the returned answer, expandable tool calls
and links to incidents in `incident_ids`. Chat requires the backend's MCP and
LLM services. API failures are shown in the UI rather than replaced with fixtures.

Contract fixtures live under `tests/fixtures/web` and can be regenerated with
`uv run python scripts/make_mocks.py`; they are not served by core.
