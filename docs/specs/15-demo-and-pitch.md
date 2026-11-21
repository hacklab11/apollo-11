# 15 Demo and pitch

Owner: P3. Tasks: T24 (demo runner script, dev), T25 (pitch assets, P3).
Judging: 20% demo. The demo must show: a fault, an innocent change, a new metric, the autonomous response, and an easy-to-follow graph, decisions, evidence, and report.

## 1. Demo script (6–7 min)

Pre-state: world at Black Friday 00:00, 28 days of history, clock running at 5 s per sim hour, LLM mode `live` (fallback `replay`).

| Time | Who talks | What happens on screen | Point to make |
|---|---|---|---|
| 0:00–0:40 | P3 | Graph, live feed, 4 agents green (incl. SellerOS at its own address) | "Nobody asks it anything. It decides what to watch." Edge types: formula, breakdown, causal. |
| 0:40–2:00 | P3 | Click I1 (Black Friday) and S7 (checkout-web release) | Sessions +40% (planned) → cleared as INNOCENT. GMV falls → Detective trail: platform drill-down, funnel decomposition, iOS control, PSP-2 refuted, `checkout-web v5.8`, client errors. Verdict CONFIRMED, rollback suggested, lost €/h. |
| 2:00–4:00 | Dev 2 | Inject S6 | Totals green. Sentinel finds small-seller share falling (divergent segment). Detective: 1P price cut trigger, repricer loop, asks SellerOS over A2A, finds 3 sellers with no floor below cost, hijacker S-99. One MIXED story with shares, planned part striped. SellerOS ack: "paused repricer for S-03, S-07, S-12". |
| 4:00–5:00 | Dev 1 | Add metric `refund_rate` with SQL only | Cartographer trail: dry run, identity `refunds / orders` validated, breakdowns, causal link from Buy Box share with 72 h lag (hypothesis). New node appears. |
| 5:00–5:40 | Dev 1 | Clear S7 | Incident → persisting → resolved after 3 normal ticks. No duplicate alerts during the incident (occurrences counter). |
| 5:40–6:40 | Dev 2 | Claude Code / Claude Desktop connected to `/mcp` | "What is broken right now and who is affected?" → MCP tool calls, answer with incident ids. Then same question in the UI chat. |
| 6:40–7:00 | P3 | Architecture slide | A2A for agents across companies, MCP for any agent, Docker + one URL. |

`scripts/demo_run.py --script demo` (T24) can drive the same timeline automatically (inject at fixed sim hours) for the backup video and for rehearsals.

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

Charts in slides: use the exported Parquet/CSV (`eye-sim export`) — GMV vs Black Friday plan for S7; small-seller GMV share for S6.

## 4. README (repo root, T24 writes it, P3 edits)

Sections: one-paragraph summary, screenshot, how to run (dev + Docker), architecture diagram, the algorithm (copy slide 4 as text), scenarios table, A2A and MCP endpoints, eval results, limitations.
