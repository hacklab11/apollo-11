# 05 Metric registry

Files: `config/metrics.yaml`, `config/identities.yaml`, `config/causal_seeds.yaml`. Loader and engine: `packages/data/src/eye/registry/`. Tasks: T06 (registry + engine), T09 (graph uses it).
P3 writes the plain-English `description` of every metric. Dev 1 writes the SQL.

## 1. SQL contract for `kind: sql`

A metric SQL returns one row per (ts, dims...) with columns:
- `ts` (timestamp), then one column per entry in `dimensions`, then `num` (double) and optionally `den` (double).
- Value at any aggregation level = `SUM(num) / SUM(den)` (or `SUM(num)` when there is no `den`).
- The SQL must not filter on time. The engine wraps it:

```sql
SELECT ts, {group_cols}, SUM(num) AS num, SUM(den) AS den
FROM ( {metric_sql} ) m
WHERE ts >= ? AND ts < ? {segment_filters}
GROUP BY ts, {group_cols}
```

This contract handles ratios correctly at every level and lets the Cartographer test any new SQL the same way.

## 2. `kind: formula`

`expression` over other metric names with `+ - * /` and parentheses, for example `gmv / orders`.
The engine computes the input series at the same segment and applies the expression per ts.
Allowed dimensions = the intersection of the inputs' dimensions.

## 3. Engine API (`registry/engine.py`)

```python
query_metric(name, start_ts, end_ts, segment: dict = {}, group_by: list[str] = []) -> list[Series]
metric_value(name, ts, segment={}) -> float | None      # one hour
metric_window(name, start_ts, end_ts, segment={}) -> float  # aggregated over the window
```
- Unknown dimension for the metric → `ValueError` with the allowed list.
- Hours with `partition_status.loaded_rows < expected_rows` keep their value but carry `completeness < 1.0` in `SeriesPoint`.
- Results are cached per (name, start, end, segment, group_by) until the next tick.

## 4. Metric list (seed)

`D` = dimensions. `c` = country, `p` = platform, `cat` = category, `t` = seller_tier, `ch` = channel, `adv` = advertiser_id, `psp`.

### Outcomes and finance

| Name | Unit | Kind | D | Definition |
|---|---|---|---|---|
| `gmv` | eur | sql | c p cat t ch | `SUM(gmv)` from orders_hourly |
| `orders` | count | sql | c p cat t ch | `SUM(units)` from orders_hourly |
| `aov` | eur | formula | c p cat t ch | `gmv / orders` |
| `fee_revenue` | eur | sql | c p cat t ch | `SUM(fee_revenue)` |
| `first_party_margin` | eur | sql | c p cat ch | `SUM(gmv - cogs_1p)` where seller_tier = '1P' |
| `platform_discounts` | eur | sql | c p cat t ch | `SUM(platform_discount)` |
| `fulfillment_cost` | eur | sql | c p cat ch | `SUM(fulfillment_cost)` |
| `ad_revenue` | eur | sql | c cat adv | `SUM(spend)` from ads_hourly |
| `marketing_cost` | eur | sql | c | `SUM(spend)` from marketing_hourly |
| `refund_cost` | eur | sql | c cat t | `SUM(refund_cost)` from refunds_hourly |
| `refunds` | count | sql | c cat t | `SUM(refunds)` from refunds_hourly |
| `contribution_margin` | eur | formula | c | see identity F1 |
| `contribution_per_order` | eur | formula | c | `contribution_margin / orders` |

### Traffic, search, feed

| Name | Unit | Kind | D | Definition |
|---|---|---|---|---|
| `sessions` | count | sql | c p | `SUM(sessions)` from traffic_hourly |
| `searches` | count | sql | c p cat | `SUM(searches)` from search_hourly |
| `search_ctr` | ratio | sql | c p cat | num `SUM(result_clicks)`, den `SUM(searches)` |
| `search_relevance` | ratio | sql | c p cat | num `SUM(relevance * searches)`, den `SUM(searches)` |
| `search_conversion` | ratio | sql | c p cat | num orders with channel='search' (join on ts,c,p,cat), den searches |
| `feed_impressions` | count | sql | c p | `SUM(impressions)` from feed_hourly |
| `feed_ctr` | ratio | sql | c p | num clicks, den impressions |
| `feed_purchase_rate` | ratio | sql | c p | num orders channel='feed', den feed clicks |
| `feed_unavailable_exposure_share` | ratio | sql | c p | num impressions where in_stock = false, den impressions |

### Checkout and payments

| Name | Unit | Kind | D | Definition |
|---|---|---|---|---|
| `carts` | count | sql | c p | `SUM(carts)` from checkout_hourly |
| `cart_rate` | ratio | sql | c p | num carts, den sessions (join traffic_hourly) |
| `cart_to_checkout_rate` | ratio | sql | c p | num checkout_starts, den carts |
| `checkout_to_payment_rate` | ratio | sql | c p | num payment_attempts, den checkout_starts |
| `payment_success_rate` | ratio | sql | c p psp | num successes, den attempts (payments_hourly) |
| `payment_latency_p95` | ms | sql | psp | max of latency_p95_ms (use `num = latency × attempts`, `den = attempts`) |
| `checkout_conversion` | ratio | formula | c p | `orders / carts` |
| `client_error_rate` | ratio | sql | p | num errors, den sessions |

### Marketplace and ecosystem

| Name | Unit | Kind | D | Definition |
|---|---|---|---|---|
| `buybox_share` | ratio | sql | cat t | num GMV of orders by tier, den all GMV (same cat) |
| `small_seller_gmv_share` | ratio | sql | c cat | num GMV where tier='small_3P', den GMV |
| `price_index_vs_1p` | index | sql | cat | num `SUM(price/ref_price)` over 3P Buy Box offers, den count; compared with 1P's own index |
| `price_change_rate` | ratio | sql | cat t | num offers whose price changed vs previous hour, den offers |
| `active_small_sellers` | count | sql | cat | distinct small_3P sellers with ≥ 1 order in the hour (use `num = 1` per seller-hour row) |
| `seller_hhi` | index | sql | cat | Herfindahl index of seller GMV shares (SQL with window functions; `den` absent) |

### Supply

| Name | Unit | Kind | D | Definition |
|---|---|---|---|---|
| `in_stock_rate` | ratio | sql | cat t | demand-weighted: num `SUM(popularity × in_stock)`, den `SUM(popularity)` over offers holding or eligible for the Buy Box |
| `stockout_skus` | count | sql | cat | SKUs with no in-stock offer |
| `po_fill_rate` | ratio | sql | cat | num delivered_qty, den qty, by delivered day (1P) |
| `po_qty_vs_forecast` | ratio | sql | cat | num PO qty, den (avg_daily_sales_7d × 10 − stock) at order time |
| `supplier_delay_hours` | hours | sql | cat | num `SUM(delivered_ts - eta_ts in hours)`, den count of deliveries |
| `fba_blocked_units` | count | sql | t | `SUM(blocked_units)` from fba_capacity_daily |

### Ads

| Name | Unit | Kind | D | Definition |
|---|---|---|---|---|
| `ad_impressions` | count | sql | c cat adv | `SUM(impressions)` |
| `ad_clicks` | count | sql | c cat adv | `SUM(clicks)` |
| `ad_spend` | eur | sql | c cat adv | `SUM(spend)` (same as ad_revenue; kept for the ads view) |
| `avg_cpc` | eur | sql | c cat adv | num spend, den clicks |
| `ad_ctr` | ratio | sql | c cat adv | num clicks, den impressions |
| `budget_pace_ratio` | ratio | sql | adv | num `spent_today`, den `daily_budget × planned_cum_share` (last hour of each advertiser) |

### Demo additions (not in the seed; added live through `add_metric`)

| Name | How it is added | Expected result |
|---|---|---|
| `fee_per_order` (live demo, S8) | SQL with num = `SUM(fee_revenue)`, den = `SUM(units)` from orders_hourly, dimensions c p cat t ch | Cartographer finds identity `fee_revenue = orders * fee_per_order` (validated), breakdowns c, p, cat, t, ch, causal hypothesis from `aov` (lag 0). During S8 its anomaly is attached to the open S8 incident. Not `average_selling_price`: that is a duplicate of `aov`, because every order is 1 unit. |
| `refund_rate` (S6, tests) | SQL with num = refunds (by c, cat, t), den = orders | Cartographer finds identity `refund_rate = refunds / orders` (validated), breakdowns c, cat, t, causal hypothesis from `buybox_share` (small/hijacker) with lag 48–96 h |
| `delivery_delay_hours` (optional) | SQL over purchase_orders | ambiguous: causal to `in_stock_rate` or to `refund_rate`? |

## 5. Identities (`config/identities.yaml`)

```yaml
- id: F1
  text: "contribution_margin = fee_revenue + ad_revenue + first_party_margin - platform_discounts - fulfillment_cost - marketing_cost - refund_cost"
- id: F2
  text: "gmv = orders * aov"
- id: F3
  text: "orders = carts * checkout_conversion"
- id: F4
  text: "carts = sessions * cart_rate"
- id: F5
  text: "checkout_conversion = cart_to_checkout_rate * checkout_to_payment_rate * payment_success_rate"
- id: F6
  text: "ad_spend = ad_clicks * avg_cpc"
- id: F7
  text: "ad_clicks = ad_impressions * ad_ctr"
- id: F8
  text: "contribution_per_order = contribution_margin / orders"
```

F5 holds only at segments where all three are defined (c, p); payment success has `psp` too. The validator checks at total and at (c, p).
Channel split: `orders` = sum over `channel` is a breakdown, not an identity.

## 6. Causal seeds (`config/causal_seeds.yaml`)

Candidates for bootstrap. The Cartographer validates each one and may add or reject. Format: `src → dst (lag hours) — rationale`.

```
search_relevance → search_ctr (0)                 — irrelevant results get fewer clicks
search_relevance → search_conversion (0)
search_conversion → orders (0)
feed_unavailable_exposure_share → feed_purchase_rate (0)
feed_purchase_rate → orders (0)
in_stock_rate → orders (0)
in_stock_rate → search_conversion (0)
po_fill_rate → in_stock_rate (24)
po_qty_vs_forecast → in_stock_rate (48)
supplier_delay_hours → in_stock_rate (24)
fba_blocked_units → in_stock_rate (48)
price_index_vs_1p → buybox_share (0)
price_change_rate → price_index_vs_1p (0)
price_change_rate → aov (0)                        — price changes move the average price (S8)
buybox_share → small_seller_gmv_share (0)
in_stock_rate → buybox_share (0)
buybox_share → in_stock_rate (6)                   — small sellers that win the Buy Box at low prices sell out
in_stock_rate → avg_cpc (6)                        — out-of-stock sellers bid more
avg_cpc → ad_revenue (0)
sessions → carts (0)
marketing_cost → sessions (0)
platform_discounts → contribution_margin (0)
cart_to_checkout_rate → orders (0)
client_error_rate → cart_to_checkout_rate (0)
payment_success_rate → orders (0)
buybox_share → refund_cost (72)                    — new low-price sellers can raise refunds
```

Cycle on purpose: `price_index_vs_1p → buybox_share → … → in_stock_rate → avg_cpc` plus `in_stock_rate → buybox_share`. The Detective must handle cycles (`09-core-agents.md` §3.4).

## 7. Tests (T06)

1. Every seed metric runs at total and at each declared dimension for 24 h with no error.
2. `orders` at total = sum of `orders` grouped by country (exact).
3. Ratio aggregation: `search_ctr` at total = `SUM(result_clicks)/SUM(searches)`, not the mean of segment ratios.
4. Formula metric `aov` = `gmv / orders` per ts.
5. Unknown dimension → `ValueError`.
6. Every identity in `identities.yaml` has residual < 0.5% on baseline data (this test lives in T08 because it uses `validate_identity`).
