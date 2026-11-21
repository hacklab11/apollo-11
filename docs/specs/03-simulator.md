# 03 Simulator

Goal: a seeded, hourly marketplace that behaves like a real one. Faults come from switches (`04-scenarios.md`). S6 comes out of seller rules, not from a drawn curve.
Code: `src/eye/sim/`. Tasks: T01, T02, T03 (baseline), T04, T05 (scenarios).

All numbers below are starting values. Tasks may tune them, but must keep the baseline tests (§9) green and must write the final values in `config/scenarios.yaml` or in constants with a comment.

## 1. Clock and backfill

- Sim "now" starts at `sim.start_ts` (default `2026-11-27T00:00:00Z`, Black Friday).
- `backfill()` generates `sim.history_days` (28) days of hourly data before "now", with no scenario active. Same code path as `step()`.
- `step()` generates exactly one hour: the hour `[now, now+1h)`, writes it with `ts = now`, then `now += 1h`.
- One `numpy.random.Generator(seed)` for the world. Each hour uses `Generator(seed + hour_index)` for noise, so a re-run with the same inject times gives identical data.

## 2. Static world (`world.py`)

### 2.1 SKUs (40)

| Category | SKUs | `ref_price` range (EUR) | Share of demand |
|---|---|---|---|
| electronics | E01–E12 | 60–600 | 0.35 |
| home | H01–H10 | 20–150 | 0.25 |
| fashion | F01–F10 | 15–120 | 0.25 |
| toys | T01–T08 | 10–80 | 0.15 |

Per SKU: `popularity` (Zipf, s = 1.1, rank inside category), `attractiveness` 0.5–1.5 (feed appeal), `supplier_id` (SUP-1..SUP-6), `cogs_ratio` 0.65–0.80 (1P cost / ref price).
Three "hyped" SKUs (E11, F09, T07) have attractiveness 1.4–1.5 and start out of stock with no restock. They are always unavailable. They matter for S4.

### 2.2 Sellers (30 + 1 dormant)

| Tier | Count | Ids | Rating | Notes |
|---|---|---|---|---|
| 1P | 1 | `1P` | 0.99 | sells 20 SKUs (E01–E10, H01–H10); has purchase orders; no FBA limits |
| large_3P | 5 | L-01..L-05 | 0.93–0.98 | 6–10 SKUs each; FBA; high capacity |
| small_3P | 24 | S-01..S-24 | 0.85–0.97 | 2–4 SKUs each; FBA; tight capacity |
| small_3P (dormant) | 1 | S-99 | 0.90 | hijacker; inactive until S6 |

Each SKU has 3–5 offers. Every electronics SKU E01–E10 has 1P + at least 2 small_3P offers.

Seller-private attributes (cost, floor, repricer mode) are generated here but written only to SellerOS's data file (`11-selleros-external.md`). The platform DB never stores them.

### 2.3 Platform-side offer state

`price`, `stock`, `inbound` (list of `(arrival_ts, qty)`), `fba_capacity_week` (units), `repricer_mode`, `floor` (None allowed), `cost`.

## 3. Demand (`demand.py`)

### 3.1 Sessions

`sessions[country, platform, h] = base × w_country × w_platform × hour_profile[hour] × dow[weekday] × campaign_mult × marketing_mult × lognormal(0, 0.05)`

- `base = 20000` sessions per hour at the peak hour, all segments together.
- `w_country`: DE 0.6, FR 0.4. `w_platform`: android 0.45, ios 0.35, web 0.20.
- `hour_profile` (00..23): `0.25 0.15 0.10 0.08 0.08 0.12 0.25 0.45 0.60 0.65 0.68 0.70 0.75 0.72 0.70 0.70 0.75 0.82 0.90 0.97 1.00 0.95 0.75 0.45`
- `dow` (Mon..Sun): `1.00 0.98 0.97 1.00 1.05 1.15 1.20`
- `campaign_mult` = 1.0, or 1.4 when I1/S6 is active. I3 has no session uplift (`campaign_mult = 1.0`).
- `marketing_mult` = 1.0, or the S5 multiplier.

Session split: search 55%, feed 25%, other 20%.

### 3.2 Search

- `searches = search_sessions × 1.6`, split by category demand share.
- `relevance[country]` baseline 0.85, noise ±0.01.
- `result_clicks = searches × 0.45 × (relevance / 0.85)`.
- A result click lands on a SKU of that category, chosen by popularity among SKUs that have an in-stock offer. Out-of-stock SKUs get 30% of their popularity (they still show in results).

### 3.3 Feed (For You)

- `impressions = feed_sessions × 12`, spread over SKUs by `exposure_weight[sku]`.
- Baseline `exposure_weight = attractiveness × popularity × (0.05 if no in-stock offer else 1.0)` (the inventory filter).
- `clicks[sku] = impressions[sku] × 0.04 × attractiveness`.

### 3.4 Conversion

For each landed click on SKU k:
- No in-stock offer → no order. With probability 0.3 the shopper tries the next SKU in the same category (same rules, once).
- Else the shopper goes through the checkout funnel for the Buy Box offer at price `p`:
  1. add to cart: `p_cart = 0.12 × price_factor × relevance_factor`
     - `price_factor = clip((ref_price / p) ^ 1.5, 0.5, 2.0)`
     - `relevance_factor = relevance / 0.85` for search clicks, 1.0 for feed and other.
  2. checkout start: `p_start = 0.75 × checkout_ok[platform]` (`checkout_ok` = 1.0 in baseline; S7 changes it)
  3. payment attempt: `p_attempt = 0.90`
  4. payment success: `p_success = psp_success[psp]` (PSP-1 0.97, PSP-2 0.96; traffic split 60/40)
  - Baseline cart → order ≈ 0.75 × 0.90 × 0.965 ≈ 0.65.
- "Other" sessions add to cart at `0.03` for a popularity-weighted in-stock SKU, then the same steps 2–4.
- Orders are 1 unit. Use expected values plus Poisson/binomial noise per cell, not one draw per shopper.

### 3.5 Client apps and errors

- Platform `web` and the checkout screen of `android` both load the shared web checkout bundle `checkout-web`. `ios` uses a native checkout. This fact is in the static table `app_components` (platform, component, source), so agents can find it.
- `client_errors_hourly`: per (ts, platform, app_version, page, error_type) count. Baseline: Poisson with mean `sessions × 0.002` spread over pages `home`, `search`, `product`, `cart`, `checkout` and error types `NetworkError`, `TimeoutError`, `TypeError`, `ResourceLoadError`.
- App version adoption: `web` and `checkout-web` switch to a new version at release time (100%). `android`/`ios` app releases roll out 20% per day.

## 4. Marketplace (`market.py`)

### 4.1 Buy Box

Among in-stock offers of a SKU: lowest `price × (1 + 0.5 × (1 − rating))` wins. Ties: higher rating.

### 4.2 Repricers (run every hour, before demand)

`lowest_other` = lowest price of other in-stock offers. Repricers act only on a big gap, so the baseline stays stable:

| Mode | Rule |
|---|---|
| `fixed` | never changes price |
| `match` | if `lowest_other < price × 0.97`: `price = max(lowest_other, floor)` |
| `beat` | if `lowest_other < price × 0.97` or (not Buy Box and `lowest_other ≤ price`): `price = max(lowest_other × 0.99, floor)` |
| recovery (all non-fixed) | if `lowest_other > price × 1.03` and price < ref_price: `price = min(lowest_other × 0.99, ref_price)` |

- Floor: `cost × 1.05`. Three small sellers (S-03, S-07, S-12) have `floor = None` → hard minimum `0.40 × ref_price`.
- Modes: 1P `match` (with floor = cogs); large_3P `match`; small_3P: 60% `match`, 40% `beat`.
- Baseline start prices: 1P at ref_price; others at ref_price × U(1.00, 1.06). Nobody is 3% below another, so no repricer fires.

### 4.3 Stock and restock (3P)

- Start stock: 3P offers 3 days of expected baseline sales. 1P: 10 days.
- Restock rule (daily at 06:00): if stock + inbound < 2 days of 7-day average sales, send inbound `qty = 7 days of average sales`, limited by remaining `fba_capacity_week`. Arrival after 48 h.
- `fba_capacity_week`: large_3P 3× baseline weekly sales; small_3P 1.3× baseline weekly sales.
- Blocked units (wanted but over capacity) go to `fba_capacity_daily.blocked_units`.

### 4.4 1P purchasing

- Daily at 02:00 per 1P SKU: `qty = max(0, avg_daily_sales_7d × 10 − stock − open_po_qty)`.
- `eta = ordered + 48h`. Delivery noise: ±6 h. `system_version = "purchasing v2.2"`.
- Deliveries add to stock at `delivered_ts`.

### 4.5 Seller ad bidding

Each advertiser seller has `bid` (EUR). If one of its offers has been out of stock for > 6 h in a row, it multiplies `bid` by 1.25 every 6 h, up to 2.5× its base bid. When stock is back for 24 h, the bid returns to base.

## 5. Ads and marketing (`ads.py`)

### 5.1 Sponsored products

- 8 advertisers: ADV-1..ADV-8. Each maps to a seller (ADV-1..ADV-5 → L-01..L-05, ADV-6..ADV-8 → S-02, S-05, S-09) and one category.
- Daily budget: 400–3000 EUR. Base bid: 0.40–1.60 EUR.
- Ad slots per search: 2. Ad impressions per category per hour = `searches × 2 × 0.9`.
- Auction per category per hour: advertisers who are not throttled compete; slots go to the 2 highest bids; CPC = next highest bid + 0.01 (reserve 0.10).
- CTR 0.02. `spend = clicks × cpc`. Ad clicks land on the advertiser's SKUs (adds to their result clicks).
- Ad spend is platform revenue (`ad_revenue`).

### 5.2 Pacing controller

- `planned_cum_share[h]` = cumulative `hour_profile` up to hour h, normalized to 1.0 at 23:00.
- An advertiser takes part in an hour's auction with probability `clip(1 − (spent_today / budget − planned_cum_share[h]) × 5, 0, 1)`. When `spent_today ≥ budget`, it stops.
- `pacing-svc v4.0` is the baseline version.

### 5.3 Platform marketing (paid acquisition)

- `marketing_hourly`: channels `paid_social`, `search_ads_external`. Baseline spend 1500 EUR/hour × hour_profile, split DE 60 / FR 40.
- `attributed_sessions` = 20% of sessions.

## 6. Finance (`finance.py`)

Per order row:
- `gmv = units × price`
- 3P: `fee_revenue = gmv × take_rate[category]` (electronics 0.08, home 0.15, fashion 0.17, toys 0.15)
- 1P: `cogs_1p = units × ref_price × cogs_ratio`; `first_party_margin = gmv − cogs_1p`
- `platform_discount` = platform-funded coupon value (0 in baseline; I1/S6 and I3 use 10% electronics coupons, with I3 reusing I1 logic for 24 h)
- `fulfillment_cost` = 1P units × 5.0 EUR
- Refunds: each order has refund probability 0.03 (hijacker offers: 0.35). Refunds appear 48–96 h later in `refunds_hourly`. `refund_cost = refunded_gmv × 0.25`.

`contribution_margin = fee_revenue + ad_revenue + first_party_margin − platform_discounts − fulfillment_cost − marketing_cost − refund_cost`

## 7. Data pipeline (`pipeline.py`)

- Every written hour gets a `partition_status` row per (table, country): `expected_rows`, `loaded_rows`, `loaded_at` (sim ts).
- Baseline: `loaded_at = ts + 1h`, `loaded_rows = expected_rows`.
- I2 changes this (`04-scenarios.md`).

## 8. DuckDB schema (`store.py`)

File: `data/world.duckdb`. Tables (all `ts` = start of the sim hour, UTC):

| Table | Grain | Columns |
|---|---|---|
| `skus` | sku | sku, category, ref_price, popularity, attractiveness, supplier_id |
| `sellers` | seller | seller_id, tier, rating, joined_ts |
| `traffic_hourly` | ts, country, platform | sessions, search_sessions, feed_sessions, other_sessions |
| `search_hourly` | ts, country, platform, category | searches, result_clicks, relevance |
| `feed_hourly` | ts, country, platform, sku | impressions, clicks, in_stock |
| `checkout_hourly` | ts, country, platform | carts, checkout_starts, payment_attempts |
| `payments_hourly` | ts, country, platform, psp | attempts, successes, latency_p95_ms |
| `client_errors_hourly` | ts, platform, app_version, page, error_type | errors |
| `app_components` | platform, component | source (`native` or `checkout-web`), current_version |
| `external_status` | id | ts, provider, status (`ok`, `degraded`, `incident`), description |
| `orders_hourly` | ts, country, platform, category, sku, seller_id, seller_tier, channel | units, gmv, fee_revenue, cogs_1p, platform_discount, fulfillment_cost |
| `refunds_hourly` | ts, country, category, sku, seller_id, seller_tier | refunds, refunded_gmv, refund_cost |
| `offers_hourly` | ts, sku, seller_id | seller_tier, category, price, stock, in_stock, is_buybox |
| `purchase_orders` | po_id | sku, supplier_id, ordered_ts, qty, eta_ts, delivered_ts, delivered_qty, system_version |
| `fba_capacity_daily` | day, seller_id | capacity_units, used_units, blocked_units |
| `ads_hourly` | ts, advertiser_id, country | seller_id, category, impressions, clicks, spend, avg_cpc, bid, daily_budget, spent_today, planned_cum_share |
| `marketing_hourly` | ts, country, channel | spend, attributed_sessions |
| `change_log` | id | ts, system, kind, planned, scope (JSON), description, expected_effect (JSON) |
| `partition_status` | table_name, country, ts | expected_rows, loaded_rows, loaded_at |
| `scenario_log` | id | ts, scenario_id, action, params (JSON) — ground truth, hidden from agents |

`change_log.kind`: `release`, `model_deploy`, `config`, `campaign`, `price_change`.
Baseline change log: about 1 release per day in random systems (`checkout-web`, `search-ui`, `catalog-svc`, `app-android`, `app-ios`) with no effect. These are realistic noise. Agents must learn that a release near an anomaly is not proof.

Write path: one DuckDB connection in core, guarded by a lock. Readers use `con.cursor()`.

## 9. Baseline tests (task T01–T03)

1. 7 sim days with no scenario: hourly `orders` total has robust z within ±3 for ≥ 99% of hours.
2. Price index (mean price / ref_price over offers) drifts < 1% over 7 days.
3. Seasonality: hour 20 sessions / hour 04 sessions between 8 and 16.
4. Identities hold in the data: `contribution_margin` recomputed from components equals the stored sum (residual < 0.1%); `sum over countries of orders = total orders`.
5. Same seed + same inject times → identical `orders_hourly` checksum.
6. Backfill of 28 days runs in < 60 s on a laptop.
7. No scenario active → `scenario_log` is empty and no S-99 offers exist.
8. Funnel identity holds: `orders` per (ts, country, platform) = `payments_hourly.successes` summed over PSPs (residual < 0.5%).

## 10. How to generate the synthetic data

We do not hand-write data and we do not commit data files. One script builds everything from the seed in about a minute.

### 10.1 Commands (task T01 creates them)

```bash
uv run eye-sim generate --days 28 --seed 42            # writes data/world.duckdb (history only)
uv run eye-sim generate --days 28 --seed 42 \
    --play "I1@0,S7@3,S6@6"                            # also plays scenarios: id@hours-after-now
uv run eye-sim export --format parquet --out data/export/   # one file per table, for notebooks/slides
uv run eye-sim stats                                    # row counts per table + date range
```

- `generate` runs `backfill()` and then, if `--play` is set, `step()` for the number of hours needed, injecting each scenario at its hour. It does not need the LLM.
- `export` writes Parquet (or CSV with `--format csv`) per table. P3 can open them in a spreadsheet or a notebook to make charts for the pitch.
- The same command with the same seed gives the same data. Commit the command, not the data.

### 10.2 Expected volume (28 days + 3 played days, default settings)

| Table | Rows (approx.) |
|---|---|
| `orders_hourly` | 300 000 |
| `feed_hourly` | 180 000 |
| `offers_hourly` | 110 000 |
| `client_errors_hourly` | 250 000 |
| `search_hourly` | 18 000 |
| `ads_hourly` | 12 000 |
| `checkout_hourly`, `traffic_hourly` | 4 500 each |
| `payments_hourly` | 9 000 |
| others | < 5 000 each |
| total | ≈ 0.9–1.0 million rows, ≈ 30–60 MB DuckDB file |

To show "a lot of data", set `--days 60` (≈ 2 million rows). Keep 28 for development speed.

### 10.3 Data variety (for the pitch)

| Type | Tables | Real-world source it imitates |
|---|---|---|
| Business facts | orders, refunds, offers, purchase orders | data warehouse |
| Product events | traffic, search, feed, checkout | clickstream / analytics |
| Payments | payments_hourly, external_status | PSP reports and status pages |
| Engineering | change_log, client_errors_hourly, app_components | deploy log, error tracker (Sentry-like), service catalog |
| Ads and marketing | ads_hourly, marketing_hourly | ad server, marketing spend |
| Data platform | partition_status | pipeline metadata |
| Seller-side (external) | SellerOS data file | third-party seller tools |

### 10.4 Generation algorithm (summary for implementers)

```
world = build_world(seed)                     # SKUs, sellers, offers, advertisers, suppliers
for hour in history_hours + played_hours:
    scenarios.apply_due_injections(hour)      # flips switches, writes change_log/scenario_log
    market.run_repricers(hour)                # prices
    market.run_restock_and_pos(hour)          # stock, inbound, POs, capacity
    ads.update_bids_and_pacing(hour)
    traffic = demand.sessions(hour)
    search, feed = demand.search_and_feed(hour, traffic)
    funnel = demand.checkout_funnel(hour, search, feed, traffic)   # carts → starts → attempts → successes
    orders = market.assign_orders_to_buybox(hour, funnel)
    finance.book(hour, orders)                # fees, margin, discounts, refunds (lagged)
    errors = apps.client_errors(hour, traffic)
    pipeline.write_partitions(hour)           # completeness, late data
    store.append_all(hour)                    # batch insert per table
```

Use vectorized numpy per hour and one batch insert per table per hour (DuckDB `INSERT ... SELECT` from a pandas-free list of tuples or Arrow). Do not insert row by row.
