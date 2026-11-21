# 06 Metric graph and Cartographer

Code: `src/eye/graph/`, `src/eye/agents/cartographer.py`. Tasks: T09 (graph), T10 (Cartographer).
Judging weight: 40% (graph + automatic extension).

## 1. Graph store (`graph/store.py`)

- `networkx.MultiDiGraph`. Node id = metric name. Node attributes = `MetricDef` fields + `state` (normal / watch / anomaly / incident).
- Edges are `Edge` models (`02-contracts.md`), keyed by `edge.id`.
- Persist to `data/graph.json` after every change. Load it at startup if present and if its `registry_hash` equals the hash of the YAML files; else run bootstrap.
- API:

```python
class GraphStore:
    def add_metric(self, m: MetricDef) -> None
    def add_edge(self, e: Edge) -> None
    def set_edge_status(self, edge_id, status, evidence: dict) -> None
    def in_edges(self, node, types: set[EdgeType] | None = None) -> list[Edge]    # toward causes
    def out_edges(self, node, types=None) -> list[Edge]                          # toward effects
    def neighbors_upstream(self, node, depth=1) -> list[str]
    def path_exists(self, src, dst) -> bool
    def cycles_through(self, node, max_len=6) -> list[list[str]]    # simple cycles on causal + formula edges
    def view(self, node=None, depth=None) -> GraphView              # for API/MCP/UI
    def set_state(self, node, state) -> None
```

- Emit SSE `graph_changed` on every change.

## 2. Bootstrap (`graph/bootstrap.py`)

Deterministic part (no LLM):
1. Add all metrics from `metrics.yaml`.
2. For each identity in `identities.yaml`: parse the left side (target) and the right side metric names. Add one `formula` edge per right-side metric → target. Run `validate_identity`; status `validated` if residual < 0.5%, else `rejected` (and log it: a seed identity must not fail).
3. For each metric and each dimension it declares: add a `breakdown` edge (src = dst = metric). Run `validate_breakdown`; status `validated` if residual < 0.5%.
4. Impact weights (§4).

LLM part (Cartographer, bootstrap mode, T10):
5. For each causal seed: run `lagged_correlation(src, dst, max_lag=96)` on the history. Give the seed list, the numbers, and the metric descriptions to the Cartographer. It returns per seed: keep as `hypothesis` (with confidence), mark `ambiguous`, or `rejected`, with a rationale. It may propose up to 10 extra causal edges from the descriptions; each proposal gets the same numeric check.
6. Cache the result in `data/graph.json`. Bootstrap with the LLM runs once per registry change, not at every start.

Causal edges never become `validated`. Their best status is `hypothesis` with a confidence. Only the Detective's investigation can support a causal claim for one incident.

## 3. Validation tools (implemented in `tools/validate.py`, T08)

| Tool | Method | Pass rule |
|---|---|---|
| `validate_identity(identity, start, end, segments)` | compute both sides per hour at total and at each shared dimension value; residual = `|lhs − rhs| / |lhs|` | median residual < 0.5% and p95 < 2% |
| `validate_breakdown(metric, dim, start, end)` | for additive metrics: sum of segments vs total. For ratio metrics: `Σ num_i / Σ den_i` vs total | residual < 0.5% |
| `lagged_correlation(src, dst, max_lag, start, end, segment)` | Pearson correlation of first differences of the de-seasonalized series (value / same-hour median of last 7 days) for lags 0..max_lag; return best lag, corr, n | none (input to the LLM) |
| `test_new_metric_against(metric, candidate_identity)` | same as `validate_identity` for an identity proposed by the Cartographer | as above |

Validation uses the last 14 days of history before "now" by default, and never includes hours with active scenarios (read the scenario timeline from the orchestrator, not from `scenario_log` through a tool).

## 4. Impact weights

`impact_weight[m]` = how much one unit of relative change in `m` moves `contribution_margin`. Simple rule for 24 hours:
- `contribution_margin` = 1.0.
- For formula edges: weight of a component of a sum = |component mean| / |target mean| × target weight. Of a product factor = target weight. Of a divisor = target weight.
- Causal edges pass `confidence × 0.5 × target weight`.
- A node's weight = max over its out-edges. Run from the top down until no weight changes (the graph has cycles; cap at 10 passes).
- Ecosystem metrics (`small_seller_gmv_share`, `active_small_sellers`, `seller_hhi`) get a floor of 0.3. Reason: small-seller health is a long-term business risk that the short-term contribution formula does not show. Write this reason in the README; judges like explicit choices.

## 5. Cartographer agent: `add_metric` flow (T10)

Input: `AddMetricRequest` (name, description, SQL or expression). The contributor gives no position.

Steps (each step writes a `TrailStep`, actor `cartographer`):

1. Guard: name pattern, `sqlguard` (`01-architecture.md` §6). Fail → return 400 with the reason.
2. Dry run: run the SQL for the last 7 days at total. Infer `dimensions` from the returned columns. Check that `num` exists. Infer `unit` if missing (LLM may suggest).
3. LLM call with tools. The Cartographer receives:
   - the new metric (description, SQL, dims, 24 sample values at total),
   - a compact catalog of existing metrics (name, unit, description, dims), max 60 lines,
   - the existing identities.
   It must propose: identities that include the new metric (as target or component), breakdowns (from dims), causal edges with lag and rationale. It may call the tools below to test ideas before it answers.
4. Tools available to the Cartographer: `query_metric`, `validate_identity` (with a candidate identity string), `validate_breakdown`, `lagged_correlation`, `get_graph`.
5. Deterministic acceptance:
   - identity: `validated` if the pass rule holds; `rejected` if residual > 5%; else `ambiguous`.
   - breakdown: `validated` / `rejected` by the pass rule.
   - causal: `hypothesis` if |corr| ≥ 0.3 at the proposed lag ±6 h, or if the LLM rationale cites a formula path; else `ambiguous`.
   - Two placements both pass (for example the new metric equals an existing metric): mark both `ambiguous` and note "duplicate of X?".
6. Add the metric with `added_by: user`, `status: active`. Add the edges. Recompute impact weights. Emit `graph_changed`.
7. Return `AddMetricResult`. The Sentinel scans the new metric from the next tick.

System prompt outline (`llm/prompts/cartographer.md`):
- Role: you maintain a graph of business metrics for a marketplace. Edge types and their meanings (copy the table from `00-overview.md` §5.3).
- Prefer exact identities over causal guesses. Test before you claim. Report what you could not decide.
- Output: call `submit_edges` (strict schema = list of `EdgeProposal` + notes).
- Effort: `high`.

## 6. Ambiguity handling (what judges look for)

| Case | Behavior |
|---|---|
| New metric duplicates an existing one (residual ≈ 0 against it) | both edges `ambiguous`, note "possible duplicate of X" |
| Identity holds at total but not in a segment | `ambiguous`, evidence shows which segment fails |
| Two causal parents fit equally (|corr| within 0.05) | both `hypothesis` with lower confidence; note "competing parents" |
| SQL dims are a subset of the parent's dims | breakdown edges only for shared dims; note the missing ones |
| Metric has no data for some hours | validation ignores hours with completeness < 0.9 and reports the count |

## 7. Tests

T09:
1. Bootstrap creates formula edges for all identities F1–F8, all `validated`.
2. Breakdown edges exist for every declared dimension; ratio metrics validate with the num/den rule.
3. `cycles_through("in_stock_rate")` finds at least one cycle with the seeds.
4. `graph.json` round-trip: load → same nodes, edges, statuses.
5. Impact weights: `contribution_margin` 1.0; `orders` > 0; `small_seller_gmv_share` ≥ 0.3.

T10 (use `llm.mode=replay` with a recorded fixture for CI; one live run by hand):
1. `add_metric(refund_rate, SQL num=refunds den=orders)` → identity `refund_rate = refunds / orders` validated; breakdowns c, cat, t validated.
2. `add_metric` with SQL `DROP TABLE orders_hourly` → rejected by the guard, no change to the DB.
3. `add_metric` of a duplicate of `orders` → `ambiguous` with "duplicate".
4. The whole flow takes < 60 s live.
