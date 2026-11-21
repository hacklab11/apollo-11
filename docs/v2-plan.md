# Eye of the System v2.0: deterministic core, minimal UI

Status: proposal. Based on `main` at `8a4540a` and the [Windows audit](audit-windows.md).

## 1. Why change

The data and reasoning layers are sound. The control layer around them is too agentic for a 24-hour build and a live demo.

What works and stays:

- The simulator, the DuckDB warehouse schema and the scenario injector (`src/eye/sim`, `src/eye/schema.py`).
- The metric registry with SQL, dimensions and formulas (`src/eye/registry`, `config/metrics.yaml`, 48 metrics).
- The typed metric graph with formula, breakdown and causal edges and edge statuses (`src/eye/graph`, 162 edges).
- The deterministic analysis tools, about 2,000 lines in `src/eye/tools`:
  - `stats.py`: expected values, baseline comparison, anomaly scans, divergent segments.
  - `drill.py`: decomposition, mix/rate split, drill-down, segment comparison, first change.
  - `validate.py`: identity and breakdown validation, lagged correlation.
  - `context.py`: freshness, the change log, planned-effect checks.
- The incident store with deduplication and the open, persisting and resolved lifecycle (`src/eye/incidents`).

What makes it hard to run and demo:

| Problem | Evidence |
| --- | --- |
| An LLM sits in every decision. | 8 call sites: Sentinel, Detective, Skeptic, Reporter, Cartographer (×2), chat, specialists. SellerOS adds its own. |
| Investigations are long and unreliable. | Recorded runs hold 71 and 94 LLM responses (`tests/fixtures/llm/s3.jsonl`, `d6.jsonl`). Both free-model evals failed. |
| The model supplies the numbers that decide CONFIRMED. | `explained_share` and contributions come from the Detective submission (`agents/detective.py:161,187`). |
| Replay is fragile. | The cache key hashes full messages, so any tool change or time-zone offset is a miss, and a miss goes live. |
| Four processes, two protocols. | Core, specialists (A2A), SellerOS (A2A) and MCP. Each needs a port, a key and a startup order. |
| It does not run on Windows. | `fcntl`, reserved ports 8000–8265, host time zone in DuckDB cursors, locale file encoding. |
| Slow feedback. | The test suite takes about 5 minutes (12,800 lines of tests) and depends on 15 recorded LLM fixture files that any tool change can invalidate. |
| The UI depends on everything above. | No offline mode since #45; idle without agents; Cytoscape and Chart.js load from CDNs. |
| The process produced integration churn. | 56 commits in about 5 hours, several follow-up fix PRs, no CI, modules loaded with optional imports so missing parts fail quietly. |

## 2. Principle

The code investigates; the language model is optional.

- Detection, investigation, the stop rule, contribution numbers and incident lifecycle are deterministic. Same input, same output, no key needed.
- The LLM may do two things, each with a deterministic fallback that is the default:
  1. Propose candidate edges for a new metric. The fallback proposes them from names, units and SQL column lineage. Validation decides either way.
  2. Rewrite a finished report in plain language. The fallback is the template. Numbers are copied, never generated.
- `llm.provider: none` is the default. The demo never depends on a network call.

## 3. Scope

### Scenarios: 11 → 6

| Keep | Role in the demo | Current ID |
| --- | --- | --- |
| Android and web checkout release breaks payments | Main fault; formula, funnel and breakdown reasoning, release timing | S7 |
| German search ranker release | Breakdown plus causal edge to a release | S3 |
| Ads pacing bug | Ads subgraph | S1 |
| Stock-out from a short purchase order | Inventory subgraph | S2 |
| Planned sale (Black Friday) | Innocent change | I1 |
| Late French partitions | Data delay; verdict "cannot conclude" | I2 |

Cut: S4 (For You feed), S5 (contribution squeeze), S6 (Black Friday hijacker), S8 (the €9 headphones and SellerOS), I3 (coupon day). S5 can come back as the optional cross-system finale once the rest works.

Trade-off: S8 is the current demo story. Cutting it removes SellerOS, which is the only consumer of the external-agent path. The judging brief asks for a fault, an innocent change and a new metric; S7, I1 and a live `add_metric` cover that.

### Components

| Component | v2 decision |
| --- | --- |
| `sim/` | Keep. Drop the cut scenarios. Snapshot the 6-week history to Parquet once and load it at startup. |
| `registry/`, `graph/`, `tools/`, `incidents/` | Keep. Add a `funnel` edge type and a `data_dependency` check (see 4.2). |
| `agents/sentinel.py` | Replace the LLM triage with a rule (4.3). |
| `agents/detective.py`, `skeptic.py` | Replace with one deterministic investigator (4.4). |
| `agents/reporter.py` | Keep the template path; the LLM rewrite becomes optional. |
| `agents/cartographer.py` | Keep validation; make the heuristic edge proposer the default. |
| `specialists/`, `a2a_client.py` | Keep the A2A servers, agent cards and client. Replace the LLM turn in `answer()` (`specialists/app.py:43`) with a fixed playbook per skill. Mount them inside core so they need no extra port (see 9.2). |
| `external/selleros/` | Keep only as a rule-based A2A responder for one demo moment (see 9.2). Not on the critical path. |
| `mcp_server.py` | Keep, started by a flag. It only reads the core API, so it costs nothing to run. |
| `api/chat.py` | Freeze. Not started by default. |
| `llm/` | Shrink to one optional client with a schema-constrained call and a fallback. Delete record and replay. |
| `web/` | Replace with the minimal UI (section 5). |
| `scripts/dev.sh` | Replace with `uv run eye`: one process, one port, works on Windows. |

## 4. Algorithm

### 4.1 Data

Hourly data for 6 weeks with hour-of-week seasonality, generated once with a fixed seed and saved as Parquet in `data/`. Startup loads it into in-memory DuckDB with `SET TimeZone = 'UTC'` on every connection. The live clock then steps one hour at a time. Scenarios modify the generated hours from their injection time.

### 4.2 Graph

Edge types: `formula`, `breakdown`, `funnel` (ordered stages), `causal_hypothesis` (with confidence). Data freshness and completeness are node properties checked before anything else; a separate `data_dependency` edge type is not needed.

Today's graph (from a running core): 48 metrics, 162 edges. 113 validated breakdowns across 7 dimensions, 22 formula edges from 8 identities (`config/identities.yaml`), 26 causal edges, all at confidence 0.5 and status `hypothesis` because startup refinement needs an LLM. Additions, smallest and most valuable first:

1. **System nodes.** Add 6–8 nodes for the systems that change: search-ranker, checkout-web, checkout-android, pacing-controller, purchasing, PSP-1, PSP-2. Link each with an `affects` edge to the metrics it drives. Releases and incidents in `change_log` attach to a system, so the investigator can walk metric → system → release. This is how a first cause becomes a graph node instead of a table row.
2. **Causal confidence from data.** At startup, run `lagged_correlation` on clean history for every causal edge and set confidence and lag from the result. Difference the series first so shared trends do not create false links. Label the result "temporally consistent", never "proven". Change-point co-occurrence with a release is the stronger evidence and raises confidence further.
3. **Missing identities.** Add formulas for the search funnel (searches × click-through × conversion), ads pacing (spent today ÷ (daily budget × planned share)) and the sessions split (search + feed + other). Today these scenarios can only use causal edges.
4. **Funnel tags.** Mark the checkout stages in identity F5 and the new search funnel as `funnel`, so the UI can draw them and the investigator can name the broken stage.

New metric pipeline:

1. Parse the SQL for source tables and columns. Propose candidate edges from shared columns, matching dimensions and formula patterns over existing metrics. Optionally ask the LLM for more candidates.
2. Validate each candidate with the existing tools: formulas by residual against history (under 1%), breakdowns by summing segments, causal candidates by lagged correlation and behaviour during past incidents.
3. Mark passing edges `validated` and the rest `hypothesis` or `ambiguous` with low weight.
4. Add the metric to monitoring and report "added X, linked to Y as formula, 2 ambiguous edges".

### 4.3 What to watch

Expected values already use a median baseline with weekday scaling and a robust spread (MAD × 1.4826, `tools/stats.py:396-417`). Keep it. Add a persistence rule: alert only when |z| ≥ 3 in 2 of the last 3 hours, which removes single-hour blips. Exclude the newest hour from the baseline window, since it may already contain the incident. With 4 or more weeks of history every hour-of-week has at least 4 samples; below that, fall back to a static threshold. The simulator runs in UTC, so daylight-saving changes do not apply.

Attention score per node: business importance × surprise × data completeness. Importance comes from the € impact weights in `graph/` and the distance to orders and contribution. Scan roots every tick; scan children only when a parent moves; add a small fixed exploration budget over the remaining nodes.

Triage rule, replacing the Sentinel LLM:

- Completeness below 0.9 or late partitions: `defer`, verdict "cannot conclude yet".
- A matching planned event explains the move (`planned_effect_check`): `innocent`.
- Matches an open incident's fingerprint: `attach`.
- Otherwise, z ≥ 3 and € impact above a floor: `investigate`.

### 4.4 Investigation

Best-first search over a hypothesis ledger, all deterministic:

1. Open the incident at the anomalous node. Check freshness and the change log first.
2. Expand graph neighbours. Order the frontier by explained delta × edge confidence ÷ query cost.
3. Formula edges: log-ratio decomposition for products and quotients (already in `tools/drill.py:79-91`); plain deltas for sums such as contribution margin; exact Shapley values only for a formula that mixes both. State the method in the report.
4. Funnel edges: find the stage whose conversion broke.
5. Breakdown edges: rank single dimensions by explanatory power and surprise (as Adtributor does). Ignore segments below 5% of the delta. Then test pairs of the top values across dimensions (for example Android × DE) and keep a pair only if it explains more than either value alone. `drill_down` already marks the segments covering 80% of the delta; the pair search is new.
6. Causal edges: the change point must follow the release or delivery; compare with unaffected segments.
7. Stop when one hypothesis explains at least 70% of the delta (the current `confirm_share`) with two independent evidence groups and no competitor above 20%. Otherwise stop at the step budget with verdict HYPOTHESIS and the top candidates.
8. Label downstream nodes fully explained by an upstream node as symptoms. The first cause is the deepest node with a trigger such as a release or a short purchase order.

Every number in the report comes from a tool result. The trail records each step with its inputs, outputs and the reason it was chosen. Every report states its stop reason: confirmed, budget exhausted, innocent, or data issue.

### 4.6 Shared context (blackboard)

All roles read and write one in-process board, so investigations can run in parallel without duplicating work:

| Piece | Purpose |
| --- | --- |
| Claims | Before investigating, a role claims (node, segment, window). If the node is upstream or downstream of an existing claim (`graph.path_exists`), it joins that incident as a symptom. The incident store already deduplicates this way. |
| Hypothesis ledger | One per incident. Skeptic, specialists and the investigator add evidence for or against. Entries carry a version; writers merge evidence instead of overwriting. |
| Evidence cache | Tool results keyed by tool, arguments and data watermark; parallel investigations share one query. |
| Event log | Append-only record of every decision and tool call. The UI streams it; roles can subscribe to it. |

Each investigation is an asyncio task. The tools run in a thread pool with one DuckDB cursor per task. Set `orchestrator.pause_sim_during_investigation` to `false`, because investigations read only data before their watermark.

### 4.5 Reporting

Fingerprint by root node and segment; deduplicate; track open, persisting and resolved; escalate only on a material change. Lead every report with three lines, following Datadog Watchdog: root cause, first failure (the earliest node that moved) and impact. Below that it has a headline, € impact per hour, an evidence table, the contribution waterfall, the trail, a confidence label (supported or hypothesis) and what remains uncertain.

## 5. UI

One page served by core, plain HTML and JavaScript, no build step. Cytoscape and Chart.js are vendored into `web/vendor/` so it works offline.

- **Top strip:** 6–8 marketplace KPI tiles (GMV, orders, conversion, payment success, ad spend, contribution, in-stock rate, sessions). Each shows the value, delta against expected, a sparkline, a status colour and a freshness chip.
- **Left:** collapsible metric tree with status dots and anomaly-score badges.
- **Centre:** the graph. Edge style shows the type; the active investigation's path is highlighted as steps arrive.
- **Right:** incident feed. Each card shows the headline, € impact, confidence, a waterfall and the trail.
- **Controls:** play, pause, step one hour, inject scenario, clear scenario, add metric.

The UI works when nothing is wrong: tiles move with seasonality and the feed says what is being watched.

## 6. Runtime budget

| Step | Today (measured on Windows) | v2 target |
| --- | --- | --- |
| Start to ready | 25 s, plus a crash without a key before #55 | under 5 s from the Parquet snapshot |
| One simulated hour including scan | 5 s timer plus agent time | under 200 ms |
| One investigation | dozens of LLM calls (71–94 per recorded scenario run) | under 1 s, no network |
| Test suite | about 5 minutes; fails on Windows | under 30 s on Linux and Windows |
| Processes and ports | 4 and 4 | 1 and 1 |
| API key | needed for incidents | not needed |

## 7. Plan

Work in this order. Each phase ends with `uv run eye` working and CI green on Linux and Windows.

| Phase | Work | Size | Done when |
| --- | --- | --- | --- |
| P0 | Windows fixes (`fcntl`, UTC cursors, UTF-8, configurable port), CI on `ubuntu-latest` and `windows-latest` | S | Current tests pass on both |
| P1 | Cut scenarios; Parquet snapshot; single `eye` entry point | S–M | Ready in under 5 s; 6 scenarios inject and clear |
| P2a | Graph additions (4.2): system nodes, causal confidence from data, three identities, funnel tags | S | Every causal edge has a data-derived confidence; S7 has a path orders → payment_success_rate → checkout-android → release |
| P2b | Deterministic triage (4.3), investigator (4.4) and blackboard (4.6) on the existing tools | M | Golden test per scenario: expected root node, verdict and share, under 1 s each |
| P2c | Benchmark: 20+ seeded fault injections across the kept scenarios | S | Precision, recall and the rank of the true cause, printed by one command and shown on a slide |
| P3 | Template reports; heuristic edge proposer for new metrics | S | `add_metric` attaches `payment_timeout_rate` with one ambiguous edge, no key |
| P4 | New UI (section 5) | M | Demo script runs in a browser with the network off |
| P5 | Showpieces 1, 3, 4 and 6 from 9.2 (rule-based A2A specialists mounted on core, swimlane, MCP flag, edge-confidence update) | S | S7 trail shows an A2A call; MCP answers "what is broken right now?" |
| P6 | Delete unused code paths (LLM agent loops, specialist prompts, record and replay, `dev.sh`) | S | Under 30 s test suite |
| P7 (optional) | Showpieces 2 and 5: rule-based SellerOS, one live LLM call with fallback; S5 cross-system scenario | S–M | Same golden tests pass with the provider on or off |

Team rules for v2:

- Integrate behind one entry point from the first PR. No optional imports for missing modules.
- One golden end-to-end test per scenario; it is the merge gate.
- `contracts.py` changes happen once, in P2, in a single PR, per AGENTS.md.

## 8. Demo script (about 5 minutes)

1. Idle: tiles move, the feed is quiet.
2. Inject I1, the planned sale. Orders and traffic rise; the incident closes as innocent with the calendar entry as evidence.
3. Inject S7. The path lights up from orders through checkout and payment success to Android and web; the release log supplies the trigger; the report quantifies the share and € impact per hour.
4. Add `payment_timeout_rate`. The graph attaches it, shows validation results and flags one ambiguous edge.
5. Inject I2. The system defers and says it cannot conclude.
6. Clear S7. The incident moves to resolved after three normal hours.

## 9. Audit against the judging brief

### 9.1 Coverage

| Brief asks for | v2 covers it with | Gap and cheap fix |
| --- | --- | --- |
| **Graph (40%)**: outcomes linked to components and breakdowns | 48 metrics, formula, breakdown and causal edges, metric tree | None |
| Relationships with distinct meanings | Edge type picks the method: decomposition, drill-down, timing test | Show the method name on each trail step |
| New metric placed without the contributor saying where | Heuristic proposer plus validation (4.2) | None |
| Validate formulas and dimensions | `validate_identity`, `validate_breakdown` | Show residuals in the add-metric result |
| Ambiguous relationships | `ambiguous` and `hypothesis` edge statuses | Add one metric that is ambiguous on purpose to the demo |
| Maintain the graph | Edges are static after startup | After a confirmed incident, raise the confidence of the causal edge it used; show the change. Small. |
| **Autonomy (40%)**: continuous loop | Clock, scan, triage, investigation, report | None |
| Decide what to watch | Attention score (4.3) | Show a "watched this hour" list with scores. It makes the decision visible. |
| Where to look next | Frontier ordering (4.4) | Show the scored frontier on each step ("why this next") |
| Missing data | I2, freshness first | None |
| Innocent changes | I1 with `planned_effect_check` | None |
| Competing explanations | Hypothesis ledger | Keep the PSP-2 decoy in S7 so one explanation is refuted on screen |
| Quantify contributions | Log-ratio decomposition, waterfall | None |
| Supported cause vs hypothesis | Stop rule and confidence label | None |
| Symptoms vs first cause | Labelling rule (4.4 step 8) | Colour symptom nodes differently in the graph |
| Notices what nobody asked | Exploration budget, divergent-segment scan | Show one exploration pick in the watch list |
| Inspiration scenarios: ads, inventory, search, For You, cross-system | S1, S2, S3 | S5 is the brief's cross-system case and a pure formula problem. Keep its injector and add it as a stretch goal after S7. S4 stays cut. |
| **Demo (20%)**: fault, innocent change, new metric | Demo script (section 8) | None |
| Easy to follow | Three panes, trail, waterfall | Keep the agent-role swimlane (9.2 item 3) |

### 9.2 Showpieces that cost little

Each item reuses code that already exists. None of them is on the critical path; drop any that slips.

| # | Show | Cost | How |
| --- | --- | --- | --- |
| 1 | **A2A specialists answer the Detective.** Growth, Marketplace and Discovery stay real A2A agents with agent cards. | S | Keep `specialists/app.py` and the A2A client. Replace `run_agent` in `answer()` with a fixed playbook per skill that calls the same Data API tools and turns thresholds into findings. Mount the A2A apps on core so they share its port. The trail shows the A2A request and the findings. |
| 2 | **External agent closes the loop.** Reporter notifies SellerOS; SellerOS acknowledges and acts. | S | Keep the A2A notification path. Replace the SellerOS LLM with a rule: on a price-error alert, check its own price history and revert. Use it in S2 or an optional S8. |
| 3 | **Named agent roles.** Sentinel, Detective, Skeptic and Reporter appear as a swimlane with hand-offs. | XS | They are stages of the deterministic pipeline. Keep the `actor` field on trail steps and render it. |
| 4 | **MCP: ask Claude Code "what is broken right now?"** | XS | `mcp_server.py` exists and only reads the core API. Start it with a flag. The presenter's own Claude Code supplies the model. |
| 5 | **One live LLM moment.** Cartographer proposes edges for the new metric, or Reporter rewrites a report. | S | Keep one call with a schema-constrained output. Fall back to the heuristic or the template. `reporter.py` already rejects any number not present in the input. |
| 6 | **The graph learns.** A confirmed incident raises the confidence of the causal edge it used. | XS | One update in the incident lifecycle; the graph view already redraws on `graph_changed`. |

Agent cards are already served at `/.well-known/agent-card.json` (`specialists/app.py:128`). Treat remote cards as untrusted input: never copy their descriptions into an LLM prompt unchecked.

Rule for all of them: label rule-based agents as rule-based in the agent card and the README. The demo may simplify the logic, but it must not show canned output as live reasoning. Judges score whether the decisions are real.

## 10. Risks

- **Less "agentic" on paper.** The judging brief rewards autonomous decisions, not LLM calls. The search policy, stop rule and trail show the decisions; the optional LLM step can be shown once.
- **Investigator quality.** The Detective prompt holds domain knowledge that the rules must now encode. Golden tests per scenario limit this.
- **Contract changes.** Removing actors and adding `funnel` and system nodes changes `contracts.py`. Do it once, early.
- **The stop rule is a design choice.** No published standard exists for "70% explained with two independent evidence groups". Defend it with the P2c benchmark, not with citations.
- **Lagged correlation is not causation.** It shows timing only; a shared driver can produce the same result. Keep causal edges labelled as hypotheses unless a release or delivery event supports them.

## 11. Sources

Research by the team using Perplexity; links not independently re-checked.

- Datadog Watchdog root cause analysis: <https://www.datadoghq.com/blog/datadog-watchdog-automated-root-cause-analysis/>, <https://docs.datadoghq.com/watchdog/rca/>
- LinkedIn ThirdEye: <https://www.linkedin.com/blog/engineering/analytics/analyzing-anomalies-with-thirdeye>
- RiskLoc and comparison with Adtributor, iDice, Squeeze: <https://arxiv.org/html/2205.10004v1>; reference implementations: <https://github.com/shaido987/riskloc>
- Squeeze: <https://netman.aiops.org/~peidan/ANM2022/8.AnomalyLocalization/LectureCoverage/2019ISSRE_Squeeze.pdf>
- Granger causality limits: <https://casrai.org/guides/granger-causality>, <https://pmc.ncbi.nlm.nih.gov/articles/PMC10571505/>
- Shared-canvas claims for agents: <https://www.honeycomb.io/blog/multi-agent-collaboration-on-shared-canvas>
- Seasonal baselines: <https://docs.dynatrace.com/docs/dynatrace-intelligence/reference/ai-models/seasonal-baseline>
- A2A agent cards: <https://a2a-protocol.org/latest/tutorials/python/3-agent-skills-and-card/>, <https://github.com/a2aproject/a2a-samples>
