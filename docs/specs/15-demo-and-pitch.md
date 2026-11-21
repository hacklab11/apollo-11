# 15 Demo and pitch

Owner: P3. Tasks: T24 (demo runner script, dev), T25 (pitch assets, P3).
Judging: 20% demo. The demo must show: a fault, an innocent change, a new metric, the autonomous response, and an easy-to-follow graph, decisions, evidence, and report.

## 1. Demo script (3 min): "The €9 headphones"

Scenario: S8 during I1 (`04-scenarios.md` S8, combined run D6). One fault (the price typo), one innocent change (the Black Friday campaign with coupons), one new metric (`fee_per_order`).

Pre-state: world at Black Friday 09:00 with I1 active since 00:00 (`--play "I1@0"`), 28 days of history, clock at 5 s per sim hour, LLM mode `live` (fallback `replay`).

### 1.1 The story in plain words

It is Black Friday. The market planned a campaign: more visitors and 10% coupons on electronics, paid by the market. Electronics sales go up, as everyone expected.

At 10:00 one small seller makes a typo in its seller tool. Its headphones should cost about €99, but now they cost about €9. The tool saves this as the real price. The shop shows €9, and checkout charges €9. Nothing on the website is broken. The price itself is wrong.

Customers find the cheap headphones and buy many of them. The newest hour of data arrives only partly loaded. A jump in half-loaded data can be noise, so the system waits. One hour later the data is complete and the jump is still there, so the system starts to investigate.

Sales are up, and with the campaign it looks like a great day. But the market earns almost nothing on €9 headphones: its own shop loses those sales, and the seller pays only a small fee. Other sellers' price robots follow the price down. A few hours later the seller sells out, and the best-offer button moves to more expensive sellers.

The system cannot know that it was a typo. It sees facts:
1. One seller's price on its headphones fell by about 90% at 10:00.
2. No campaign in the calendar explains it. The coupons are only 10%.
3. The new price is far below the other sellers' prices and far below this seller's own prices in the last 28 days.
4. The seller's price changed first. Other prices, sales, stock and the best-offer button changed after it.

So it says: "An unplanned price drop by one seller is the first cause. It may be a pricing error. This is a hypothesis." Then it asks the seller's own agent (SellerOS): "Was this price change intended?" SellerOS answers: "No. It was a manual entry below the seller's cost." The hypothesis is now confirmed. The seller fixes the price. Sales and earnings return to normal, and the incident closes.

### 1.2 Script

| Time | Who talks | On screen | What the speaker says |
|---|---|---|---|
| 0:00–0:30 | P3 | The graph | "This is our market as a graph. Money from sales = number of orders × average price: that is a **formula**. Sales split by category and by seller group: that is a **breakdown**. A price change can change sales: that is a **possible cause**. Nobody asks our system anything. It decides what to watch." |
| 0:30–0:55 | P3 | Black Friday is running; inject S8; the first S8 hour is deferred | "It is Black Friday. Visitors and sales are up. The system **expected** this, because the campaign is in the calendar. Now headphone sales jump. The newest hour of data has only partly arrived, so the system **waits** instead of raising an alarm on half the data. One hour later the data is complete and the jump is confirmed." |
| 0:55–1:45 | Dev 2 | The alert and the investigation trail | "Sales are up, but the money we earn on electronics fell. The system follows the formula: orders went up, and the average price went down. It follows the breakdown: electronics, then small sellers, then one seller. It compares explanations. The planned campaign and its coupons explain [X]%. That part is innocent. An unplanned price drop of about 90% by one seller at 10:00 explains [Y]%. That is the **first cause**. It may be a pricing error, but the system marks that as a **hypothesis**, because it cannot know the seller's intent. The other sellers' price drops, the sold-out stock and the moving best-offer button came later, so they are **symptoms**." |
| 1:45–2:25 | Dev 1 | Add metric `fee_per_order` with SQL only; the Cartographer trail; the new node | "Now we add a metric the system has never seen: the money the market earns from seller fees per order. We give only the query. The system checks that fees = orders × this metric, so the formula is confirmed. It finds breakdowns by category and seller group, and it links the metric to the average price as a possible cause. The new metric immediately shows the drop, and the system **adds it to the open incident as new evidence**. There is no second alert." |
| 2:25–3:00 | Dev 2 | The incident page: occurrences counter, verdict, SellerOS answer, status | "One incident, updated every hour, and no repeated alerts. The system asked the seller's own agent: was this price change intended? The answer: no, it was a manual entry below cost. The hypothesis is now **confirmed**. The seller fixed the price. Sales and earnings return to normal, so the incident becomes **resolved**." |

[X], [Y] and the price numbers come from the best rehearsal. Do not invent them.

### 1.3 How the script covers the judging rules

| Rule (`../hackathon-rules.md`) | Moment |
|---|---|
| Metric graph: formula, breakdown, possible cause | 0:00, and each edge type used again in the trail at 0:55 |
| Extend understanding | 1:45: SQL only; formula validated; breakdowns and a causal hypothesis found; monitored at once |
| Monitor: groups, expected behaviour, seasonality, completeness, planned events | 0:30: campaign expected; half-loaded hour deferred. 0:55: drill-down to one seller group |
| Investigate: next step, edges, competing explanations, stop, symptom versus first cause | 0:55: formula → breakdown → campaign versus price drop with shares → symptoms named |
| Communicate and adapt: numbers, trail, persists or resolves, no repeats | 1:45: new evidence attached to the open incident. 2:25: occurrences counter, `resolved` |
| Supported cause versus hypothesis | 0:55: hypothesis. 2:25: confirmed by SellerOS |

### 1.4 Not in the 3 minutes

S1–S7, the MCP client, the chat and A2A details are not in the live script. Show them in Q&A if asked. S6 is the deep example on slide 5.

`scripts/demo_run.py --script demo` (T24) drives the same timeline automatically for the backup video and for rehearsals.

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
