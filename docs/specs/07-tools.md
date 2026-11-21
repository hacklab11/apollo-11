# 07 Deterministic tools

Code: `src/eye/tools/`. Tasks: T07 (stats, drill, context), T08 (decompose, validate).
Rule: tools compute; agents decide. Every tool is a plain Python function with pydantic input and output, so the LLM runtime (`08-agent-runtime.md`) can expose it as a Claude tool with a JSON schema.
Every tool output has a `summary: str` field (≤ 300 chars, with the key numbers). The trail shows this text.

## 1. Baseline

`expected(metric, ts, segment)`:
- Take the values at the same hour of day on the same weekday in the last 4 weeks (4 points). Expected = median. Spread = MAD × 1.4826.
- If fewer than 3 points have completeness ≥ 0.9, fall back to the same hour on the last 7 days, scaled by the day-of-week ratio (mean daily total of this weekday / mean daily total of all days, over the history).
- `z = (actual − expected) / max(spread, 0.02 × |expected|, ε)`. The floor stops tiny spreads from making huge z values.
- Ratio metrics: compute expected for num and den separately, then expected ratio = exp_num / exp_den.
- Planned events are not removed from the baseline. The Skeptic handles them.

## 2. Tool list

| Tool | Input | Output | Used by |
|---|---|---|---|
| `query_metric` | name, start, end, segment, group_by | list of `Series` | all |
| `baseline_compare` | name, start, end, segment | per hour: actual, expected, lower/upper (± 3 spread), z; window totals; summary | Sentinel, Detective, Skeptic |
| `scan_anomalies` | ts | top N `Anomaly` (kind `point`) over all active metrics × (total + one- and two-dimension segments) with \|z\| ≥ `sentinel.z_threshold` | Sentinel (prefetched) |
| `scan_divergent_segments` | ts, window_hours=6 | `Anomaly` (kind `divergent_segment` or `share_shift`) | Sentinel (prefetched) |
| `drill_down` | metric, dim, start, end, base_start, base_end, segment | per value: actual, expected, delta, share_of_total_delta, z; sorted by \|share\| | Detective |
| `decompose` | metric, start, end, base_start, base_end, segment | `Contribution` rows along formula in-edges, sum of shares = 1 | Detective |
| `mix_rate_split` | ratio metric, dim, windows, segment | `Contribution` rows of kind `mix` and `rate` per segment | Detective |
| `compare_segments` | metric, dim, value_a, value_b, windows | both deltas side by side (control group check, e.g. iOS vs web) | Detective, Skeptic |
| `first_change` | metrics list, segment, start, end | per metric: first hour where \|z\| ≥ 3 for 2 hours in a row | Detective (symptom vs cause) |
| `check_freshness` | tables or metric, start, end, segment | per hour completeness, late hours, verdict `ok` / `late` / `incomplete` | Sentinel, Skeptic |
| `list_events` | start, end, system?, kind?, country?, planned? | `change_log` rows + `external_status` rows | Detective, Skeptic, specialists |
| `planned_effect_check` | event id, metric, segment, window | observed change vs `expected_effect`; share of delta the event explains | Skeptic |
| `get_graph` | node?, depth? | `GraphView` | all agents |
| `get_incidents` | status? | list of incident summaries | Sentinel, Reporter |
| `validate_identity`, `validate_breakdown`, `lagged_correlation` | see `06-graph-and-cartographer.md` §3 | | Cartographer |

## 3. Algorithms

### 3.1 `scan_anomalies(ts)`
- For each active metric, compute z at `ts` for the total, each value of each declared dimension, and each observed combination of values for every pair of declared dimensions.
- To avoid noise, skip count segments with an expected value below 20 and euro segments with less than €200 in expected hourly euros. Use the expected numerator for per-unit euro metrics (e.g. fee revenue for fees per order); z still uses the per-unit value.
- `impact_eur_per_hour` = |delta| × impact_weight × eur_scale, where eur_scale = 1 for eur metrics, = mean contribution per unit for counts (contribution_margin mean / orders mean for `orders`-like metrics), and = mean contribution_margin per hour × relative change for ratio metrics.
- `priority = |z| × max(impact_weight, 0.05) × completeness`.
- Return the top `sentinel.top_k × 3` by priority. The Sentinel sees the top `top_k`.

### 3.2 `scan_divergent_segments(ts, window_hours=6)`
For additive metrics over the last `window_hours`:
- `divergent_segment`: segment z ≤ −3 while total z ≥ −1 (or the mirror for up). Example: orders of small_3P fall while total orders rise.
- `share_shift`: share of a segment in the total moves by ≥ `sentinel.divergence_pp` percentage points against its expected share.
These are how S6 is found while the totals look healthy.

### 3.3 `decompose(metric, ...)`
Use the identity whose target is `metric` (formula in-edges).
- Sum `Y = Σ a_i`: contribution of `a_i` = Δa_i; share = Δa_i / ΔY. Sign of subtracted terms is negative.
- Product/quotient `Y = Π x_i^(e_i)` (e = +1 or −1): log attribution. `share_i = e_i × ln(x_i'/x_i) / ln(Y'/Y)`; contribution = share × ΔY. If |ln(Y'/Y)| < 1e-6, return equal-split with a note.
- Add a `residual` row so shares sum to 1.0.
- Windows: current = [start, end); baseline = same hours one week earlier, or the explicit base window.

### 3.4 `mix_rate_split(ratio_metric, dim, ...)`
Shift-share for a ratio R = Σ s_i × r_i, s = segment share of the denominator:
- mix effect = Σ (s_i' − s_i) × r_i
- rate effect = Σ s_i' × (r_i' − r_i)
Return both per segment and total. Mix + rate = ΔR exactly.

### 3.5 `drill_down`
For each value of `dim`: delta and `share_of_total_delta`. Mark the smallest set of values that covers ≥ 80% of the total delta as `focus`. Example output summary: "DE holds 94% of the drop in search_conversion; FR −1% (normal)."

### 3.6 `first_change`
Scan hour by hour from `start`. The first hour with |z| ≥ 3 for two hours in a row is the change point. Used to order causes in time: the earliest upstream change is the trigger candidate.

### 3.7 `check_freshness`
Join `partition_status` for the tables behind the metric (`MetricDef.sql` table names, parsed once at load). Completeness = loaded_rows / expected_rows. Verdict `incomplete` if any hour < `sentinel.min_completeness`; `late` if `loaded_at − ts > 2h`.

### 3.8 `planned_effect_check`
Parse `expected_effect` values like `"+40%"` or `25000`. Observed effect = actual / expected_without_event − 1 over the event window (expected from the baseline, §1). Explained share = min(1, planned / observed) when both have the same sign; 0 when signs differ. Return verdict `matches` (observed within ±25% of planned), `exceeds`, `below`, `opposite`.
Example S7: planned orders +35%, observed −10% → `opposite`. The event does not explain the drop.

## 4. Domain tools for specialists (data API)

Served by core at `/data/*` (`api/data_api.py`), called by specialists over HTTP (`10-a2a-and-specialists.md`). All read-only. All take `start`, `end`.

| Endpoint | Returns | Specialist |
|---|---|---|
| `GET /data/ads/pacing?advertiser_id&day` | hourly spent_today vs budget × planned_cum_share, participation | Growth |
| `GET /data/ads/auction?category` | per hour: bidders, avg bid, CPC, slots, top advertisers | Growth |
| `GET /data/marketing?country` | spend, attributed sessions per channel | Growth |
| `GET /data/discounts?category` | platform discount EUR per category/SKU | Growth |
| `GET /data/offers?sku` or `?category` | price, stock, is_buybox per offer per hour | Marketplace |
| `GET /data/buybox?category` | Buy Box share by seller and tier | Marketplace |
| `GET /data/stock?category` | in-stock SKUs, stockout SKUs, hours out of stock | Marketplace |
| `GET /data/purchase_orders?category` | POs with qty, eta, delivered, system_version | Marketplace |
| `GET /data/capacity?tier` | FBA capacity, used, blocked units | Marketplace |
| `GET /data/sellers/new?since` | sellers by joined_ts with their offers | Marketplace |
| `GET /data/search/quality?country` | relevance, CTR, conversion per category | Discovery & Experience |
| `GET /data/feed/exposure?top_n` | top SKUs by impressions with in_stock flag and attractiveness | Discovery & Experience |
| `GET /data/checkout/funnel?country&platform` | carts, starts, attempts, successes per hour | Discovery & Experience |
| `GET /data/payments?psp` | attempts, success rate, latency per PSP | Discovery & Experience |
| `GET /data/errors?platform&page` | client errors by app_version, page, error_type | Discovery & Experience |
| `GET /data/app_components` | which platform uses which component | Discovery & Experience |
| `GET /data/changes?system&kind` | change_log + external_status | all |
| `GET /data/metric?name&segment` | proxy to `query_metric` + `baseline_compare` | all |

## 5. Tests (T07, T08)

1. `baseline_compare` on baseline data: |z| < 3 for ≥ 99% of hours (orders, sessions, search_ctr).
2. After injecting S3 for 3 h: `drill_down(search_conversion, country)` puts ≥ 80% of the delta in DE.
3. `decompose(contribution_margin)` on S5: shares sum to 1.0 ± 0.02; marketing_cost and platform_discounts are the two largest negative rows.
4. `mix_rate_split` identity: mix + rate = ΔR (exact to 1e-9).
5. `scan_divergent_segments` after S6 T0+12h: returns `small_seller_gmv_share` or `buybox_share{small_3P}`.
6. `planned_effect_check` for I1 alone: verdict `matches`. With S7: `opposite` for orders.
7. `check_freshness` during I2: FR verdict `incomplete`; after backfill `ok`.
8. `compare_segments(cart_to_checkout_rate, platform, web, ios)` in S7: web −85%, iOS ≈ 0.
