# 09 Core agents: Sentinel, Lead Detective, Skeptic, Reporter

Code: `src/eye/agents/`, `src/eye/orchestrator.py`, `src/eye/incidents/store.py`. Tasks: T12 (incidents + Reporter), T13 (Sentinel), T14 (Skeptic + Detective), T18 (orchestrator).
Judging weight: 40% (autonomous monitoring and investigation).
Cartographer: `06-graph-and-cartographer.md`.

## 1. Division of work: code vs LLM

| Decision | Who | Why |
|---|---|---|
| Compute z, deltas, shares, residuals | code (tools) | numbers must be exact and repeatable |
| Which candidates become investigations | Sentinel LLM | needs context: events, open incidents, business meaning |
| Next step in an investigation | Detective LLM | open-ended search over graph + agents |
| Is it innocent? | Skeptic LLM with `planned_effect_check` | needs judgement about plans and data quality |
| Stop rule thresholds | code checks the Detective's claimed state | prevents the LLM from stopping on a weak story |
| Dedupe, merge, lifecycle | code (Reporter store) | must be predictable |
| Incident narrative | Reporter LLM | readable story with numbers |

## 2. Sentinel (T13)

Runs every tick. Input (prefetched by code, so one LLM call is often enough):
- top `sentinel.top_k` from `scan_anomalies(ts)` and `scan_divergent_segments(ts)`,
- `check_freshness` for the segments of those candidates,
- `list_events` for the last 24 h,
- open incidents (id, fingerprint, title, entry metric, segment).

Tools for extra checks: `baseline_compare`, `check_freshness`, `list_events`, `get_graph`.
Output (`submit`): `list[SentinelDecision]`, one per candidate.

Decision rules in the prompt (the LLM applies them; code does not override except where marked):
- `defer` if completeness < `sentinel.min_completeness` (code enforces this; an LLM `investigate` on incomplete data is changed to `defer`).
- `attach` if the candidate is the same metric family/segment as an open incident, or is downstream of the incident's root node in the graph (`path_exists(root_node, candidate.metric)`).
- `investigate` for the highest-priority candidates that are not attached. Several candidates that share an upstream node → investigate only the most upstream one, attach the others after the result.
- `watch` for |z| between 2.5 and 3 or a single hour spike: re-check next tick; two ticks in a row → investigate.
- `ignore` with a reason (for example "known baseline release noise, no effect on metrics").
- Planned events do not cause `ignore` here. The Skeptic decides innocence with numbers.

Effort `low`. Max steps 4.

## 3. Lead Detective (T14)

### 3.1 Input

`Anomaly` + graph neighborhood (in-edges to depth 2) + open incidents + list of available agents (from A2A cards) + config thresholds.

### 3.2 Tools

`baseline_compare`, `drill_down`, `decompose`, `mix_rate_split`, `compare_segments`, `first_change`, `query_metric`, `list_events`, `check_freshness`, `get_graph`,
`ask_specialist(agent: growth|marketplace|discovery|selleros, question, metric, segment)` (A2A, `10-a2a-and-specialists.md`),
`ask_skeptic(hypothesis_id)` (runs the Skeptic agent locally),
`update_hypotheses(list[Hypothesis])` (the Detective keeps its board here; code stores it and returns the board with step counts),
`submit(InvestigationResult)`.

### 3.3 Method (in the system prompt)

1. Measure: `baseline_compare` the entry metric. Write the delta.
2. Locate: `drill_down` over the metric's dimensions; keep the focus segment.
3. Explain arithmetically: if the metric has formula in-edges, `decompose`; for ratios, `mix_rate_split` on the focus dimension. Move to the component with the largest share.
4. Find candidate causes: follow causal in-edges of the current node; `list_events` in the window before `first_change`; ask the matching specialist one precise question.
5. Keep 2–5 hypotheses on the board. Each needs `explained_share`, evidence for, evidence against.
6. Test against controls: `compare_segments` (other country, other platform) to refute broad causes.
7. Order in time: `first_change` on the candidate nodes; the earliest upstream change is the trigger candidate. Downstream nodes that changed later are symptoms.
8. Ask the Skeptic about the top hypothesis and about any planned event in the window.
9. Stop (§3.5).

### 3.4 Loops (feedback cycles)

When the Detective reaches a node already on its path, or `cycles_through(node)` shows a cycle containing ≥ 2 changed nodes:
- record `loop = [nodes in cycle order]`,
- name the trigger = the node with the earliest `first_change` in the cycle that has a change_log event or an external cause,
- name the amplifier = the cycle,
- do not walk the cycle again; spend remaining steps on evidence for the trigger and on affected actors (ask SellerOS for seller-side facts).

### 3.5 Stop rules (code checks them on `submit`)

| Verdict | Code check | If the check fails |
|---|---|---|
| `CONFIRMED` | root hypothesis `explained_share ≥ detective.confirm_share`; evidence_for from ≥ `detective.min_independent_evidence` different `group`s; every other hypothesis with share ≥ 0.2 is `refuted` | code downgrades to `HYPOTHESIS` and adds the failed check to `open_questions` |
| `MIXED` | ≥ 2 supported hypotheses whose shares sum ≥ `confirm_share`; contributions include each | downgrade to `HYPOTHESIS` |
| `INNOCENT` | Skeptic verdict innocent with `planned_effect_check` = `matches`, or seasonality | downgrade to `HYPOTHESIS` |
| `DATA_ISSUE` | `check_freshness` verdict not ok for the window | downgrade to `HYPOTHESIS` |
| `HYPOTHESIS` | always allowed | — |

Budget end (`BudgetExceeded`) → `HYPOTHESIS` with the current board and `stop_reason: budget`.
Contributions must sum to 1.0 ± 0.02; if not, code adds a `residual` row.

Effort `high`. Max steps `detective.max_steps` (15).

### 3.6 Example trail (S7, abbreviated)

```
1 baseline_compare(gmv)                 → −38% vs expected Black Friday hour (expected includes no plan); z −7.9
2 drill_down(gmv, platform)             → web 41%, android 57%, ios 2% of the drop
3 decompose(orders) via F3/F4/F5        → cart_rate +2%; checkout_conversion −84% share 98%
4 decompose(checkout_conversion)        → cart_to_checkout_rate share 97%; payment_success_rate 1%
5 list_events(−6h..now)                 → checkout-web v5.8 (release, same hour), PSP-2 degraded, app-android v9.14 (20% rollout), black-friday-2026 (planned)
6 compare_segments(cart_to_checkout_rate, platform, web, ios) → web −86%, ios −1%
7 ask_specialist(discovery, "Do client errors or components explain web+android checkout failure?") → TypeError on checkout page, v5.8, 31k/h; android checkout loads checkout-web
8 update_hypotheses: H1 release checkout-web (0.97), H2 PSP-2 (refuted: success rate 96.1% normal), H3 app-android (refuted: web affected; no version split)
9 ask_skeptic(H1 + black-friday) → planned effect "orders +35%", observed −38%: opposite; not innocent
10 submit: CONFIRMED, rollback checkout-web to v5.7, lost GMV €61k/h
```

## 4. Skeptic (T14)

Input: one hypothesis or one anomaly + window. Tools: `list_events(planned=true)`, `planned_effect_check`, `check_freshness`, `baseline_compare` (seasonality: same hour last 4 weeks), `compare_segments`.
Output (`submit`): `{verdict: innocent | partly_innocent | not_innocent | data_issue, explained_share: float, reasons: list[str], evidence: list[Evidence]}`.
Rules: a planned event explains a change only in the direction and size it planned; anything beyond ±25% is not explained. A release without an effect plan is never innocent by itself. Effort `medium`, max steps 6.

## 5. Reporter and incident store (T12)

### 5.1 Fingerprint

`fingerprint = f"{root_node}|{canonical(segment)}|{cause_type}"`; before an investigation finishes, a provisional fingerprint `entry.metric|segment|pending`.
`canonical(segment)` = sorted `k=v` joined by `,`.

### 5.2 Match (before an investigation starts)

For a Sentinel decision `investigate`:
1. Same provisional fingerprint as an open incident → attach (occurrences += 1, last_seen updated). No new investigation.
2. Graph: candidate metric is downstream of an open incident's `root_node` and segments overlap → attach.
3. Else start an investigation; create an incident with status `investigating`.

### 5.3 Upsert (after an investigation)

- Fill the incident from `InvestigationResult`; set final fingerprint.
- If another open incident has the same final fingerprint → merge: keep the older one, move investigation ids, set `merged_into` on the newer.
- If two open incidents share a causal chain (`path_exists` between their root nodes, or the same `trigger` hypothesis) → link both in `related_incident_ids`; if one is the trigger of the other, merge the downstream one into the trigger's incident. This produces one S6 story.
- Status: `INNOCENT` verdict → `innocent`; `DATA_ISSUE` → `deferred`; else `open`.
- Reporter LLM writes `title` and `summary` from the result (effort `medium`, no tools, structured output). Code fills the numbers; the LLM may not change them.
- If `affected_sellers` is not empty and SellerOS is reachable → send `SellerAlert`; store the ack in `notifications`.

### 5.4 Lifecycle (every tick, code only)

For each incident with status `open` or `persisting`:
- compute |z| of the entry metric at the entry segment for this tick;
- |z| < `reporter.resolve_z` → `normal_ticks += 1`, else `normal_ticks = 0` and status `persisting` (if it was `open` for ≥ 2 ticks);
- `normal_ticks ≥ reporter.resolve_after_ticks` → `resolved`, `resolved_ts = ts`;
- `impact_eur_total += impact_eur_per_hour` while not resolved.
For `deferred` (data issue): when completeness is back ≥ 0.9 and |z| < resolve_z → `resolved`.
A resolved incident that comes back within 24 h with the same fingerprint is reopened (status `open`, occurrences += 1), not duplicated.

## 6. Orchestrator (T18)

See `01-architecture.md` §3. Extra rules:
- The orchestrator owns the scenario timeline it applied (for validation windows) and passes "no-scenario hours" to the Cartographer; agents never see it.
- An investigation that raises an unexpected exception → incident status `open`, verdict `HYPOTHESIS`, summary "investigation failed: <error type>", trail kept.
- Graceful shutdown: finish running tool calls, persist incidents and graph.

## 7. Tests

T12: fingerprint and match rules (unit); merge of two incidents with the same trigger; lifecycle `open → persisting → resolved`; reopen within 24 h.
T13: replay fixture for S3 tick: Sentinel returns `investigate` for the DE search candidate and `attach` or `ignore` for downstream `orders`; I2 tick → `defer` (code-enforced even if the fixture says investigate).
T14: replay fixtures per scenario (recorded once live); code stop-rule checks downgrade a weak `CONFIRMED`; budget end → `HYPOTHESIS`.
Live evals: `scripts/eval_scenarios.py` (`14-testing-and-evals.md`).
