# 15 Demo and pitch

Owner: P3. Tasks: T24 (demo runner script, dev), T25 (pitch assets, P3).
Judging: 20% demo. The demo must show: a fault, an innocent change, a new metric, the autonomous response, and an easy-to-follow graph, decisions, evidence, and report.

## 1. Demo script (3 min): "The €9 headphones"

Scenario: S8 during I3 (`04-scenarios.md`, combined run D6). One fault (the price typo), one innocent change (Electronics coupon day), one new metric (`fee_per_order`).

Pre-state: a normal day at 09:00 with I3 active since 00:00 (`--play "I3@0"`), 28 days of history, clock at 5 s per sim hour, LLM mode `live` (fallback `replay`). Do not change `sim.start_ts`; recorded replay fixtures depend on the date.

### 1.1 The story in plain words

It is a normal day in the market. The market has planned a coupon day for electronics: 10% off, paid by the market. Electronics sales go up, as everyone expected.

At 10:00 one seller makes a typo in its seller tool. Its 5 most popular headphones should cost €99, but now they cost €9. The tool saves this as the real price of the offer. The shop shows €9, and checkout charges €9. Nothing is broken on the website. The price itself is wrong.

At 10:20 customers find the cheap headphones and buy many of them. Our system sees the jump, but the 10:00–11:00 hour is not finished yet, and a jump in half an hour of data can be noise. So the system waits. At 11:00 the hour is complete and the jump is still there, so the system starts to investigate.

Sales are up, and with the coupons it looks like a great coupon day. But the market earns almost nothing on €9 headphones, and the seller loses money on every pair. By 16:00 the seller has sold out. The headphone pages show "out of stock", and the best-offer button moves to more expensive sellers. Customers leave.

The system cannot know that it was a typo. It sees facts:

1. One seller's price on 5 products went from €99 to €9 at 10:00 (−91%).
2. No campaign in the calendar explains it. The coupon day is only 10%.
3. €9 is far below the other sellers' prices and far below this seller's own prices in the last 28 days.
4. The price drop came first. Sales, stock and the best-offer button changed after it.

So it says: "An unplanned 91% price drop by one seller is the first cause. It may be a pricing error. This is a hypothesis." Then it asks the seller's own agent: "Was this price change intended?" The seller answers: "No, it was a mistake." The hypothesis is now confirmed. The seller fixes the price. Sales and earnings return to normal, and the incident closes.


The five headphones and €99 → €9 are narrative labels. The simulator uses the seller's affected electronics SKUs; prices and shares come from rehearsal (`04-scenarios.md` S8).

### 1.2 The 3-minute script

| Time | Who talks | On screen | What the speaker says |
|---|---|---|---|
| 0:00–0:30 | P3 | The graph | "This is our market as a graph. Money from sales = number of items sold × average price: that is a **formula**. Sales split by category and by seller: that is a **breakdown**. A price change can change sales: that is a **possible cause**. Nobody asks our system anything. It decides what to watch." |
| 0:30–0:55 | P3 | Coupon day starts, then headphone sales jump | "Coupon day starts. Electronics sales rise. The system **expected** this, because the coupon day is in the calendar. At 10:20 headphone sales jump. The hour is only partly filled, so the system **waits** for the full hour instead of raising an alarm on part of the data. At 11:00 the jump is confirmed." |
| 0:55–1:45 | Dev 2 | The alert and the investigation trail | "Sales are up, but the money we earn on electronics fell. The system follows the formula: the number of items sold went up, and the average price went down. It follows the breakdown: electronics, then headphones, then one seller. It compares two explanations. The coupons explain [X]% of the price drop. That is innocent. An unplanned 91% price drop by one seller at 10:00 explains [Y]%. That is the **first cause**. It may be a pricing error, but the system marks that as a **hypothesis**, because it cannot know the seller's intent. The sold-out stock and the best-offer button moving to other sellers started later, so they are **symptoms**. It stops when [Z]% of the change is explained, with two independent kinds of evidence." |
| 1:45–2:25 | Dev 1 | Add metric `fee_per_order` with SQL only; the Cartographer trail; the new node | "Now we add a metric the system has never seen: the money the market earns from seller fees per order. We give only the query. The system checks that fees = items sold × this metric, so the formula is confirmed. It finds a breakdown by seller, and it links the new metric to the average price as a possible cause. On electronics orders from third-party sellers, excluding 1P (`category = 'electronics' AND seller_tier != '1P'`), the new metric immediately shows the fee-per-order drop, and the system **adds it to the open incident as new evidence**. There is no second alert." |
| 2:25–3:00 | Dev 2 | The incident page: occurrences counter, status, seller's answer | "One incident, updated every hour, and no repeated alerts. The system asked the seller's own agent: was this price change intended? The seller answered: no, it was a mistake. The hypothesis is now **confirmed**. The seller fixed the price. Sales and earnings return to normal, so the incident becomes **resolved**." |

[X]/[Y]/[Z] and the price numbers come from rehearsal. Do not invent them. T25 replaces the placeholders. The stop line follows `09-core-agents.md` §3.5: at least 70% explained, two independent evidence groups, alternatives refuted. [Z] is the observed explained share, not a new threshold.

### 1.3 How the script covers the judging rules

| Rule | Moment |
|---|---|
| **Metric graph:** formula, breakdown, possible cause | 0:00 intro, and each link used again in the trail at 0:55 |
| **Extend understanding** | 1:45: a new metric from its query only. The formula is checked, a breakdown and a possible-cause link are found, and the metric is monitored at once. |
| **Monitor:** groups, expected behaviour, seasonality, completeness, planned events | 0:30: coupon day expected (planned event); the half-filled hour, so the system waits (completeness). 0:55: from the total down to electronics, headphones and then one seller (groups). |
| **Investigate:** next step, links, competing explanations, when to stop, symptoms versus cause | 0:55: formula → breakdown → coupons versus price drop, each with its share → stop rule → stockout and best-offer button marked as symptoms |
| **Communicate and adapt:** numbers, trail, persists or resolves, no repeats | 2:25: one incident, occurrences counter, status changes to resolved. 1:45: new evidence added to the open incident. |
| **Supported cause versus hypothesis** | 0:55: "pricing error" marked as a hypothesis. 2:25: the seller's answer confirms it. |
| **Judges' demo requirements:** fault, innocent change, new metric, autonomous response | The price typo, the coupon day, `fee_per_order`, the question to the seller and the recovery |

### 1.4 Not in the 3 minutes

S1–S7, the MCP client, the chat and A2A details are not in the live script. Show them in Q&A if asked. S6 stays the deep example on slide 5: totals can hide a hurt group.

`scripts/demo_run.py --script demo` (T24) drives the same timeline automatically for the backup video and for rehearsals.

### 1.5 What the build must support

| Item | Spec and build support |
|---|---|
| New scenario | `04-scenarios.md` S8/I3, T27: one seller's affected electronics offers × 0.10 at 10:00 during I3. Offer price history carries the time. Agent tools must not read `scenario_log`. |
| SellerOS | `11-selleros-external.md`: `price_intent_check` answers "No, it was a mistake" from `likely_error`; the alert action reverts the price. T24 calls `clear(S8)` after the ack to model the seller's fix. |
| New metric | `05-metric-registry.md`: the owner chose `fee_per_order`, absent from the starting registry. `average_selling_price` equals `aov` because every order is 1 unit; it is not a new metric. Add SQL only, confirm fees = items sold × this metric, link average price as a possible cause, attach to the open incident, no second alert. |
| Partial current hour | `04-scenarios.md` S8, T04/T27: reuse the late-partition hook for both countries, loaded share 0.5 in the hour still filling; backfill next tick. Sentinel defers, investigates at 11:00. |
| Coupons | `04-scenarios.md` I3 and `03-simulator.md`: reuse I1 coupon logic, `scenarios.I3.discount: 0.1`, platform-funded electronics coupons for 24 h, no session uplift. |
| Recovery time | `09-core-agents.md` lifecycle, `04-scenarios.md` D6, T24: resolved after 3 normal ticks, about 15 s at 5 s per sim hour. Check in rehearsal. |
| Numbers | T25 replaces [X]/[Y]/[Z] with rehearsal numbers, plus prices and shares. Do not invent them. The headphones drill is narrative, not a new simulator sub-category (`04-scenarios.md` S8). |

## 2. Backup

- Record the full demo once it passes (screen recording, 1080p, with voice). Keep it on two laptops and online.
- Keep `data/llm_cache.jsonl` from the best rehearsal; `LLM_MODE=replay` replays it.

## 3. Pitch slides (max 8)

1. Problem: a marketplace with thousands of metrics; nobody can watch them all; Black Friday makes it worse.
2. What we built (one sentence + screenshot).
3. The graph: edge types and how a new metric places itself (diagram from the UI).
4. The algorithm: what to watch (priority = surprise × impact × completeness; divergent segments), where to look (formula → breakdown → causal → specialists), when to stop (≥ 70% explained, ≥ 2 independent evidence groups, alternatives refuted). Code computes, agents decide.
5. Big and small players: S6 story and why totals hide it. Research sources (`00-overview.md` §2).
6. Open platform: A2A (SellerOS as an external company), MCP (any agent), Docker deploy.
7. Results: table from `eval_scenarios.py` (scenarios × pass, ticks to detect, LLM calls).
8. What is uncertain and what is next.

Charts in slides: use the exported Parquet/CSV (`eye-sim export`) — electronics average price and fee per order for S8; small-seller GMV share for S6.

## 4. README (repo root, T24 writes it, P3 edits)

Sections: one-paragraph summary, screenshot, how to run (dev + Docker), architecture diagram, the algorithm (copy slide 4 as text), scenarios table, A2A and MCP endpoints, eval results, limitations.
