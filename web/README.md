# Mock observatory (T21)

Plain HTML, CSS and ES modules. No install or build step. Cytoscape.js 3.30.4
and Chart.js 4.4.8 load from pinned jsDelivr URLs; Google Fonts is optional
(system sans-serif is the fallback). Internet access is needed for the charts.

From the repository root, with Python 3.13 installed via `uv`:

```sh
uv run python -m http.server --bind 127.0.0.1 -d web 5500
```

Open `http://localhost:5500/?mock=1`. The task card's
`python -m http.server -d web 5500` also works when `python` is 3.13; the
explicit bind above follows AGENTS.md. Without `?mock=1` the app offers a link
to mock mode and does not contact a backend. No credentials are needed.

## Review checklist

- Inspect the 12-node graph: solid formula edges (`=`), dashed causal edges,
  confidence-dependent causal widths, dotted amber ambiguous edges, and
  rejected edges hidden until toggled. Breakdown dimensions appear as chips.
- Click a node or use **Inspect metric** with the keyboard. Orders has an
  actual line, expected line and shaded range, plus the v5.8 release marker
  and a text summary. **Fit** resets the graph viewport.
- Open S7 and S6 in **Incidents**. Read the summary, impact, signed factor
  bars, supported/refuted hypotheses, actions and investigation trail.
  S6 includes the striped planned factor, external SellerOS step and delivery
  acknowledgement. Expand hypotheses and individual step details.
- Switch Open / Cleared / Resolved; empty tabs say they have no mock data.
- Watch the activity feed replay a JSONL event every two seconds, loop and
  retain only the latest 30. Pause / Resume stops / starts replay; Step +1h
  updates the local clock. Scenario buttons change local state only.
- Submit **Add metric**, inspect accepted/ambiguous/rejected relationships,
  notes and the full AddMetricResult, then close to see highlighted edges.
- Ask **Why did orders drop?** in chat, expand the tool calls, follow the
  incident link. All questions return the fixed mock response.
- Tab through controls; focus outlines remain visible. The graph's select
  control is its keyboard equivalent; the native modal traps focus, supports
  Escape, and restores focus to its trigger. Chart summaries convey the data
  without depending on canvas, colors or pointer interaction.

## Mock limitations and T22 handoff

The exported `api` object at the top of `app.js` is the endpoint adapter: one
method per endpoint in spec 13, including the currently unused anomalies and
demo-truth endpoints. Panel code consumes those methods, not fixture paths.
T22 should replace this boundary and connect SSE to refresh panels. T21 does
not mutate the incident/chart fixtures in response to scenario buttons or
replayed historical events. Scenario and clock controls are local previews.

T00 provides only `metric_orders.json` and `metric_def_orders.json`. Selecting
other nodes explicitly shows an **Orders reference** chart, never relabelled
as measurements for the selected metric; descriptions/dimensions still come
from the correct metric. No series are fabricated. Added metrics remain in
this browser session and also use the reference chart. Add metric always
returns `orders_per_session`, irrespective of the submitted SQL; it executes
no SQL. T00 has no S6 trail file, so its trail is extracted from the existing
`events.jsonl` TrailSteps by investigation ID. Fixtures remain unchanged.

The UI spec requests a Cleared tab, but the IncidentStatus contract has no
`cleared` member. The tab is reserved and empty rather than mapping an
unrelated state to it. Resolved matches `resolved`; Open includes
`investigating`, `open` and `persisting`.
