# Overview page

Status: implemented in `packages/web/src/components/Overview.tsx`.

The Overview tab is the web workspace's home page. It answers one question in a few
seconds: is the marketplace healthy now, and if not, what does it cost and where is
the incident? It is a way into incidents, not a general BI dashboard; Grafana stays
the place for operational metrics.

## Navigation

- Tabs, in order: **Overview** (default), **Incidents**, **Graph**.
- The header (brand, sim clock, data-complete time, Resume/Pause) does not change
  and stays visible on every tab.

## Content

### 1. Status strip

One row of four summary cards:

| Card | Value | Source |
|---|---|---|
| Open incidents | count of incidents with status `open` or `persisting` | `/api/ui/incidents` |
| Impact now | sum of `impact_eur_per_hour` of those incidents, in EUR/h | `/api/ui/incidents` |
| Monitoring | Running or Paused | `/api/ui/status` (`paused`) |
| Data freshness | "Up to date", or whole hours that `data_complete_ts` is behind `sim_ts` | `/api/ui/status` |

- When the open-incident count is above zero, the count is a button that opens the
  first open incident on the Incidents tab.
- "Open" uses the same grouping as the incident sidebar: every status except
  `recovered` and `closed`.
- When monitoring is paused, the Monitoring card says so in text, not only in color.

### 2. Key metric tiles

A grid of tiles for a fixed list of key metrics (see Assumptions). Each tile shows:

- the metric label and unit, in the same format as the incident charts;
- the latest actual value and the change against `expected`, as a percent with a
  sign and an up or down marker; a change of 5% or more uses the destructive color;
- a small chart of the last 48 hours: actual line, dashed expected line, and the
  `lower`/`upper` band when the API returns it;
- a badge when an open incident has this metric as its entry metric. The badge
  names the incident and opens it on the Incidents tab.

A tile has three states besides normal: loading, error ("Unavailable. Retrying…"),
and no data ("No measurements in this window."). One tile's failure does not hide
the others.

## Behavior

- Data refreshes on the same signals as the rest of the workspace: SSE events, with
  polling as the fallback. Metric tiles refetch every 10 seconds, like
  `MetricChart`.
- Opening an incident from the Overview selects it on the Incidents tab.
- Values with `null` show "missing", never 0.
- Times are UTC, formatted like the header.

## Style

Use the existing design system in `packages/web/src/index.css`; add no new colors
or fonts.

- Cards reuse the `.chart-card` surface (white, `rounded-xl`, border, `p-4`) on the
  `#f7f8fa` page background.
- Headings reuse `.section-heading` and `.eyebrow`; small counts reuse the
  `.group-heading span` pill.
- Chart colors match `MetricChart`: actual `#176b67` (primary), expected `#94a3b8`
  dashed, grid `#e8ebf0`; the band uses the primary color at low opacity.
- Incident badges reuse `.state-badge` and `.status-dot` with their existing
  open/recovered colors. Negative business impact uses `--color-destructive`.
- Layout follows the existing breakpoints (1600 px, 1100 px, 700 px): 4 tile
  columns on wide screens, 2 on medium, 1 on narrow.
- Icons come from `lucide-react`, like the existing tabs.

## Accessibility

- Each tile is a `section` with an accessible name; charts are `aria-hidden` and
  the tile text carries the values, like `MetricChart`.
- Change direction is shown by sign and marker, not by color alone.
- All links and buttons have visible focus (existing focus ring).

## Acceptance criteria

1. Opening http://localhost:8080 shows the Overview tab first; Incidents and Graph
   still work as before.
2. With two open incidents of 100 and 50 EUR/h, the status strip shows "2" and
   "150 EUR/h".
3. Each key metric has a tile with the latest value, the change against expected
   and a 48-hour chart.
4. A metric that is the entry metric of an open incident shows a badge; clicking it
   shows that incident on the Incidents tab.
5. When one metric request fails, that tile shows the error state and the others
   still render.
6. When monitoring is paused, the Monitoring card shows "Paused" in text.
7. `npm run lint`, `npm test` and `npm run build` pass; tests cover criteria 2, 4
   and 5.
8. No Core API change is needed; `npm run generate:api` shows no drift.

## Assumptions

- **Key metric list:** a fixed list in the frontend: `gmv`, `orders`, `aov`,
  `checkout_conversion`, `payment_success_rate`, `contribution_margin`, `sessions`,
  `ad_revenue`. Reason: the registry has 48 metrics and no "key metric" flag; a
  frontend list is the smallest change and easy to replace with a config value later.
- **Segment:** tiles show the whole marketplace (empty segment). Reason: a home page
  shows totals; segments belong to incident detail.
- **Window:** 48 hours ending at `data_complete_ts`. Reason: two daily cycles make the
  expected line readable; the API returns hourly points.
- **Badge match:** an incident matches a tile when `entry.metric` equals the tile
  metric. Reason: `entry` is the only metric field on the incident list response.

## Deferred

- **Recent activity feed** (latest sentinel decisions and investigations): there is
  no list API for it; `/api/events` is only a change signal.
- **Tile to graph node:** `CausalGraph` takes no props and cannot focus a node.
- **Key metrics from config:** move the list to `config/` when users need to change it.
