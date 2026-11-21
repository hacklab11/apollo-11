# Spec and 24h plan: Eye of the System

> Status: summary spec. Detailed specs: `docs/README.md`.

Track: "Eye of the System: Fully Agentic Monitoring". Source: `docs/hackathon-rules.md`.
Chosen design: Idea 2 "Detective Bureau", with the Lead Detective using competing hypotheses.
Team: 3 people (2 developers, 1 non-technical). Time: 24 hours.

## 1. Goal

An agentic system watches a mock marketplace with no human prompts.
The marketplace has big players (the platform's own retail, large sellers) and small sellers who interact through prices, the Buy Box, ads, and stock.
The system keeps a typed metric graph, detects meaningful changes, investigates them, and reports causes with evidence.
It also finds problems that hide inside healthy totals: one actor group loses while the total grows.
External agents connect over A2A. Any agent connects over MCP. A web UI shows the graph, anomalies, incidents, and a chat.

## 2. Real-world problems we model (research summary)

| Real problem | What happens | Where it appears in our demo |
|---|---|---|
| Repricer price wars | Sellers' bots undercut each other in a loop. A price cut by the platform's own retail gives every bot a lower target. [1][2] | S6 trigger and amplifier |
| Buy Box concentration | Almost all sales go through the Buy Box. Losing it is an existential threat for a small seller. [3] | `buybox_share[seller_tier]` |
| Price-parity suppression | The platform removes a seller from the Buy Box when it finds a lower price elsewhere. [3][4] | Optional innocent-looking cause in S6 |
| FBA capacity limits | Low capacity → stockout → lower rank → fewer sales → lower score → lower capacity. A loop. [5] | S2, S6 |
| Ad cost inflation | CPCs rose about 48% from 2019 to 2025. Small sellers bid above their margin to stay visible. [6] | S1, S6 |
| Listing hijacking | A counterfeit seller with a low price takes the Buy Box on a real listing. Returns and complaints follow later. [7] | S6 lagged effect |

Sources:
[1] https://www.thesling.org/how-algorithms-on-amazon-look-to-raise-prices-24-7-and-why-that-prime-day-deal-really-isnt
[2] https://www.repricer.com/blog/amazon-price-match/
[3] https://www.cnbc.com/2023/10/06/amazon-sellers-sound-off-on-the-ftcs-long-overdue-antitrust-case.html
[4] https://www.azag.gov/sites/default/files/2024-05/2024-05-15%20-%20AZ%20AG%20v.%20Amazon%20BUY%20BOX_PARITY%20Complaint%282440163.2%29.pdf
[5] https://sell.amazon.com/blog/announcements/fba-capacity and https://www.sellerlabs.com/knowledge-base/restock-limits-explained-ipi-score-and-fba-capacity/
[6] https://www.modernretail.co/?p=123015 and https://skai.io/blog/amazon-ads-inflation
[7] https://www.consumerreports.org/customer-reviews-ratings/hijacked-reviews-on-amazon-can-trick-shoppers and https://www.ecomengine.com/blog/amazon-listing-hijacking

Most of these sources are seller-tool vendors or news. They are good enough for a realistic simulation, not for exact numbers.

## 3. The hardest problem: S6 "Black Friday cascade"

This is our deep example for the slides and Q&A. It is what most teams will not build: a cross-actor feedback loop, where the total looks good.
The 3-minute live demo uses S8 "The €9 headphones" (`15-demo-and-pitch.md` §1): one fault, one innocent change, one new metric.

Timeline in the simulator:

| Sim time | Event | Kind |
|---|---|---|
| T0 | Black Friday campaign starts. Traffic +40%. Platform-funded 10% coupons on electronics. | Planned, innocent |
| T0+2h | Platform retail (1P) cuts price on the top 10 electronics SKUs by 15%. | Trigger |
| T0+2h… | Small sellers' repricers chase the lowest price. 3 sellers have no price floor. Prices slide below their cost. | Amplifier (loop) |
| T0+8h | Those 3 sellers sell out. FBA capacity limits block restock for 2 days. | Consequence |
| T0+10h | Out-of-stock items drop in search rank. Sellers raise ad bids. Electronics CPC +60%. | Consequence (loop 2) |
| T0+12h | A hijacker with counterfeit offers takes the Buy Box on 4 SKUs. | Opportunist |
| T0+3d | Refunds and complaints on those SKUs rise. | Lagged effect |

What the dashboards show: orders +30%, GMV +20%. Everything looks green.
What is really happening: contribution margin falls. Small-seller GMV share −35%. 3 small sellers sell below cost. Active small sellers start to fall. Refunds rise later.

Correct report:
- Innocent part: campaign traffic (planned). Quantified share.
- Trigger: 1P price cut at T0+2h (first change in time).
- Amplifier: repricer loop among small sellers. 3 sellers have no floor.
- Consequences: stockouts made worse by capacity limits. CPC inflation. Hijacker Buy Box capture.
- Uncertain: churn of small sellers (too early to confirm). Refund effect (lagged, still growing).
- Suggested actions: price-war circuit breaker on these SKUs, notify affected sellers through their external agent, Buy Box audit of the hijacker.

## 4. Architecture

```
                 ┌──────────────── Web UI (1 page) ────────────────┐
                 │ graph · metric charts · incidents · chat panel  │
                 └───────────────┬─────────────────────┬───────────┘
                                 │ REST + SSE          │ /api/chat
                                 ▼                     ▼
┌──────────────── core (FastAPI, port 8000) ────────────┐   chat agent ──MCP client──┐
│ Agent-based simulator + clock → DuckDB                 │                            │
│ Metric registry (YAML) + typed graph (networkx)        │◄──── MCP server (port 8100)┘
│ Deterministic tools (query, decompose, baseline, ...)  │      ▲ also Claude Desktop,
│ Agents: Cartographer, Sentinel, Lead Detective,        │      │ Claude Code, any MCP client
│         Skeptic, Reporter                              │
│ Incident store                                         │
└───────┬───────────────┬───────────────┬────────────────┘
        │ A2A           │ A2A           │ A2A                 A2A (both directions)
        ▼               ▼               ▼                     ◄──────────────►
   Growth          Marketplace       Discovery              External "SellerOS" agent
   specialist      specialist        specialist             (port 8300, separate folder,
   (one process, 3 Agent Cards, port 8200)                   own data, no core imports)
```

### 4.1 Agents

| Agent | Kind | Job | Main tools |
|---|---|---|---|
| Cartographer | core | Builds the graph. Places a new metric, validates edges, labels them. | `propose_edges`, `validate_formula`, `validate_breakdown`, `lagged_correlation` |
| Sentinel | core | Each tick: picks what to watch, triages anomalies, opens incidents. Also scans for hidden segments. | `scan_anomalies`, `scan_divergent_segments`, `check_freshness`, `list_events` |
| Lead Detective | core | Runs the hypothesis tournament. Picks the next step. Decides when to stop. Detects loops. | `decompose`, `drill_down`, `ask_specialist` (A2A), `query_metric` |
| Skeptic | core | Tries to clear each anomaly: planned event, stale data, seasonality. | `list_events`, `check_freshness`, `baseline_compare` |
| Reporter | core | Writes the incident. Merges related incidents into one story. Tracks persist/resolve. Suppresses duplicates. | `incident_store` |
| Growth specialist | A2A | Traffic, campaigns, ads auction, CPC, budget pacing, platform-funded discounts. | ads + campaign tables |
| Marketplace specialist | A2A | Sellers, offers, prices, Buy Box, stock, FBA capacity, wholesale POs, deliveries. | offers, inventory, PO tables |
| Discovery & Experience specialist | A2A | Search funnel, For You feed, reco model changes, checkout funnel, client errors, app/web releases. | search, feed, checkout, payments, client errors tables |
| SellerOS (external) | A2A, separate process | A third-party vendor that runs repricers and stock tools for small sellers. Knows repricer rules, seller costs, supplier delays. Receives alerts from us. | its own data file only |

Why the external agent is a seller tool: small sellers use third-party repricers and ERPs in real life. The data that proves the S6 amplifier (repricer rules, missing floors, seller cost) lives outside the platform. The platform must ask for it over A2A. The external agent can also receive our alert ("your repricer is in a price war on SKU X"). This shows two organizations' agents working together.

### 4.2 Scenario coverage

| # | Scenario | Entry anomaly | Competing hypotheses | Evidence from |
|---|---|---|---|---|
| S1 | Ad budget spent by 10:00 | `ad_spend_pace` | traffic rose / pacing ignores schedule / CPC rose | Growth |
| S2 | Category sales fall, demand stable | `orders[category]` | stockout ← late delivery / PO too small / capacity limit | Marketplace (+ SellerOS for 3P supplier delay) |
| S3 | Searches steady, buyers fewer | `search_conversion[DE]` | search release (DE) / price change / stockout | Discovery + Marketplace |
| S4 | For You clicks up, purchases down | `feed_purchase_rate` | reco model change / inventory filter off | Discovery + Marketplace |
| S5 | Orders up, contribution down | `contribution_margin` | ad cost / product mix / fees / funded discounts | Growth + Discovery + `decompose` |
| S6 | Black Friday cascade (section 3) | `small_seller_gmv_share`, found by divergent-segment scan | campaign / 1P price cut / repricer loop / capacity / hijacker | all three + SellerOS |
| S7 | Black Friday checkout outage: `checkout-web v5.8` breaks the Place Order button on web + Android webview | `gmv` below Black Friday plan, `cart_to_checkout_rate[web, android]` | front-end release / PSP-2 degraded / android app release / campaign | Discovery & Experience + change log + client errors |
| I1 | Innocent: Black Friday campaign alone | `sessions` up | planned event | Skeptic clears it |
| I2 | Missing data: late partition for FR | `orders[FR]` drop | stale data | Skeptic / Sentinel defers |

## 5. Data and metric graph

### 5.1 Simulator (agent-based for sellers)

- Python. 14 days of hourly history, then a sim clock (1 sim-hour per N real seconds, N in config).
- Dimensions: country {DE, FR}, platform {android, ios, web}, category {electronics, home, fashion, toys}, seller_tier {1P, large_3P, small_3P}.
- Catalog: about 40 SKUs. About 30 sellers. Each SKU has several offers.
- Each seller is a small rule-based actor (not an LLM): a repricer rule (match lowest − 1%, with a floor or no floor), stock, FBA capacity, ad bid rule.
- Buy Box rule: lowest price among in-stock offers, with a bonus for seller rating.
- Shoppers: sessions with seasonality (hour, weekday) and noise → search → click → cart → order on the Buy Box offer.
- Ads: second-price auction per category. CPC comes from the bids.
- Change log: releases, model deploys, campaigns (planned events).
- Each scenario is a switch: `inject(scenario_id, start_ts)` and `clear(scenario_id)`.
- S6 is not a drawn curve. It comes out of the seller rules once the trigger fires. This makes the data behave like a real loop.
- SellerOS gets its own data file (repricer configs, seller costs, supplier ETAs) from the same seed. Core never reads that file.

### 5.2 Metric registry

Each metric is a YAML entry: `name`, `description`, `sql` (or expression), `dimensions`, `unit`, `domain`.
Starting set: about 30 metrics, including ecosystem metrics:
`buybox_share[seller_tier]`, `price_index_vs_1p`, `small_seller_gmv_share`, `active_small_sellers`, `sellers_below_cost`, `seller_hhi` (concentration).

### 5.3 Graph

Nodes: metrics. Edges have a type and a status.

| Edge type | Meaning | Reasoning it supports | Validation |
|---|---|---|---|
| `formula` | child is a term in an arithmetic expression (`orders = sessions × cr`) | exact contribution split | evaluate expression on history, residual < 1% |
| `breakdown` | same metric split by a dimension | find the segment that holds the change | segment sum = total, residual < 0.5% |
| `causal_hypothesis` | A may drive B, with a lag | candidate cause, never proof alone | lagged correlation + domain rationale from LLM |

Edge status: `validated`, `ambiguous`, `rejected`, `hypothesis`. Each edge stores its evidence.
Causal edges may form cycles (price → buybox → stock → rank → ad bids → price). The graph keeps them. The Detective must handle them (section 6.2).

### 5.4 Graph extension (Cartographer)

Input: a new metric with only a name, a definition, and SQL. The contributor gives no position.

1. The LLM reads the definition, the SQL, and the current graph. It proposes candidate edges with types.
2. Tools test each candidate on history: formula residual, breakdown sums, lagged correlation.
3. The LLM decides: keep, reject, or mark `ambiguous` when two placements both pass or evidence is weak.
4. The metric joins monitoring at the next tick.

Live demo metric: `fee_per_order` (formula edge `fee_revenue = orders * fee_per_order`, breakdowns, causal hypothesis from `aov`). During S8 its anomaly joins the open incident.
Second metric for S6 and tests: `refund_rate = refunds / orders`. With it the system can see the S6 lagged refund effect.
Optional ambiguous metric: `delivery_delay_hours` (causal to `refund_rate` or to `orders`?).

## 6. Algorithms (what the judges asked to see)

### 6.1 What to monitor (Sentinel)

- `scan_anomalies`: for every node × segment, robust z-score against the same hour on the last 3 same weekdays (median + MAD).
- `scan_divergent_segments`: finds segments that move against their total (total up, segment down), and shifts in share between actor groups. This finds S6 while the totals look good.
- Priority = |z| × impact weight × completeness. Impact weight = the share of contribution margin that flows through the node.
- Segments with incomplete data are deferred, not alerted.
- The LLM gets the top 10 candidates plus the event calendar. It decides which candidates become investigations and writes why.

### 6.2 Investigation (Lead Detective, hypothesis tournament)

1. Start at the anomalous node. Record the delta versus expected.
2. Loop, max 15 steps:
   - Pick the next action: follow a `formula` edge (decompose), a `breakdown` edge (drill to the largest share), a `causal_hypothesis` edge, or ask a specialist or SellerOS over A2A.
   - Keep 2–5 hypotheses. Each has: explained share of delta, supporting evidence, refuting evidence.
   - The Skeptic checks each top hypothesis for an innocent explanation.
3. Loop handling: when the walk returns to a node it saw before, the Detective labels a feedback loop. It reports the trigger (the first node to change in time), the amplifier (the loop), and the consequences. It does not walk the loop again.
4. Stop when one of these is true:
   - `CONFIRMED`: one hypothesis explains ≥ 70% of the delta, has ≥ 2 independent pieces of evidence (for example data + change log or data + SellerOS), and the alternatives are refuted.
   - `HYPOTHESIS`: the budget is used. Report the best candidate and what is uncertain.
   - `INNOCENT`: a planned event or a data gap explains the change.
   - Mixed results are allowed. S6 must report an innocent part and a harmful part with their shares.
5. Symptom vs first cause: a node is a symptom if an upstream node changed earlier and explains its delta.

### 6.3 Reporting (Reporter)

- Incident fingerprint = (root-cause node, segment, cause type).
- A new anomaly with a known fingerprint updates the open incident. It does not create a new alert.
- Incidents that share a causal chain are merged into one story (S6 produces one story, not five alerts).
- Each tick: re-check open incidents. Status: `open` → `persisting` → `resolved`.
- An incident holds: summary, business impact (EUR/hour), affected actors, contribution table, investigation trail (each step, tool call, result, reason), confidence label, open questions, suggested actions.

## 7. Interfaces

### 7.1 A2A

- Each specialist and SellerOS publishes an Agent Card with its skills.
- The Lead Detective sends a task: question + context (anomaly, time window, segment). The agent returns an artifact: findings, numbers, confidence.
- The Reporter sends SellerOS a notification task for incidents that affect its sellers.
- Agent URLs come from config. Unreachable agent → the Detective records "agent unavailable" and continues.

### 7.2 MCP server (streamable HTTP)

| Tool | Purpose |
|---|---|
| `list_incidents(status?)` | open/persisting/resolved incidents |
| `get_incident(id)` | full report with trail |
| `get_graph(node?, depth?)` | nodes and typed edges |
| `query_metric(name, start, end, dims?)` | time series |
| `add_metric(name, description, sql)` | triggers the Cartographer |
| `inject_scenario(id)` / `clear_scenario(id)` / `list_scenarios()` | demo control |
| `system_status()` | sim clock, tick, agents online |

### 7.3 Web UI (one page)

- Left: the graph (Cytoscape.js). Node color = anomaly state. Edge style = edge type. Click a node → its chart.
- Center: metric chart with expected band and anomaly markers. Incident list. Click an incident → the trail as a timeline.
- Right: chat. `/api/chat` runs a Claude agent whose tools come from our MCP server through an MCP client.
- Top bar: sim clock, scenario buttons, "add metric" form.
- Live updates over SSE.

## 8. Acceptance criteria (demo must show these)

1. The system runs with no human input. Ticks, scans, and investigations appear in the UI.
2. Each of S1–S5, when injected, produces one incident with the correct root cause and a contribution table.
3. S7 during Black Friday: the system finds `checkout-web v5.8` on web + Android, uses iOS as the control, refutes PSP-2, and suggests a rollback.
4. S6 produces one merged story: innocent campaign share, trigger, repricer loop, consequences, uncertain items. The totals stayed green, and the system found it anyway.
5. I1 alone is labeled `INNOCENT` and does not raise an alert.
6. I2 is deferred or labeled as a data issue, not as a business fault.
7. `add_metric(fee_per_order, ...)` places the metric with validated edges in < 60 s, and the UI shows it. During S8 its anomaly attaches to the open S8 incident.
8. The same anomaly on later ticks updates one incident. After `clear_scenario`, the incident becomes `resolved`.
9. S6 uses evidence from SellerOS over A2A, and SellerOS receives a notification. Stopping SellerOS makes the report say "agent unavailable" and lowers confidence.
10. An external MCP client (Claude Desktop or Claude Code) can list incidents and read a report.
11. Chat in the UI answers "why did contribution fall?" by using MCP tools.
12. S8 during I1 (D6): the first S8 hour is deferred for incomplete data; the incident names the seller's price drop as the first cause, first as `HYPOTHESIS`, then `CONFIRMED` after SellerOS; I1 is a `planned` contribution; other price drops and the stockout are symptoms.

## 9. Where the details are

This file is the summary. The detailed files win when they differ from this file.

- Team, timeline, and Devin workflow: `../plan/implementation-plan.md`
- Task cards for Devin sessions: `../tasks/README.md`
- Demo script and pitch: `15-demo-and-pitch.md`
- Stack and versions: `01-architecture.md`
- Index of all specs: `../README.md`

## 10. Assumptions

- Seller actors are rule-based, not LLM agents. Many LLM sellers per sim-hour would be too slow and too costly for 24 hours. The agentic part is the monitoring system, which is what the judges score.
- The external agent is a seller-side tool (SellerOS), not Finance. Contribution math is a `formula` decomposition in core, so S5 does not need an outside agent. SellerOS adds data that only an outside party has, and it shows how big and small players interact.
- The 3 specialists run in one process with 3 Agent Cards. This saves setup time. Each one is still a separate A2A agent.
- All processes run on one laptop, on localhost ports. A deploy is not in the scoring.
- LLM output is not deterministic. Fixed seeds, a rehearsed order, and a backup video reduce the risk.
- Statistics are used only as tools (baseline, residuals, correlation, segment divergence). Every choice of what to watch, where to look, and when to stop is made by an agent and written in the trail.
