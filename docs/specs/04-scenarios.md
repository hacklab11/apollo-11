# 04 Scenarios (scenario bible)

This file is the ground truth. P3 owns the wording. Dev 1 owns the parameters.
Machine-readable copy: `tests/oracle/scenarios.yaml` (task T23). Parameters: `config/scenarios.yaml`.

Each scenario:
- `inject(id, at_ts=None)` starts it at the current sim hour (or `at_ts`) and writes `scenario_log`.
- `clear(id)` stops its effect from the next hour on. Effects that already happened stay in history (sold-out stock, refunds in the pipeline).
- It writes `change_log` rows only where a real company would have them. A bug does not announce itself; the release that caused it does.
- It has at least one decoy: a real-looking but wrong explanation that the agents must refute.

Oracle fields used by `scripts/eval_scenarios.py`:
`entry_metrics` (any of them may open the incident), `root_node`, `cause_type`, `segment`, `verdict`, `must_mention` (substrings, case-insensitive), `must_not_claim` (cause types that must not be the root cause), `max_ticks_to_detect`.

---

## S1 Ad budget spent by 10:00

Story: advertiser ADV-3 (L-03, electronics) spends its full daily budget by 10:00.

Injection:
- `change_log`: `{system: "pacing-svc", kind: "release", planned: false, description: "pacing-svc v4.1: new budget smoothing", scope: {"advertisers": "all"}}` at inject time.
- Effect: for ADV-3 only, the pacing controller ignores `planned_cum_share` (takes part in every auction until the budget is gone). Other advertisers are not affected, because the bug is in a code path only ADV-3's campaign type uses.

Decoys:
- Traffic is normal. Hypothesis "traffic rose" must be refuted with sessions z ≈ 0.
- CPC in electronics rises slightly (+10%) because ADV-3 is in more auctions. Hypothesis "CPC rose" explains < 20%.

Expected:
- `entry_metrics`: `budget_pace_ratio` segment `{advertiser_id: ADV-3}` or `ad_spend`.
- `root_node`: `budget_pace_ratio`; `cause_type`: `pacing_bug`; verdict `CONFIRMED`.
- `must_mention`: `ADV-3`, `pacing`, `v4.1`, `schedule`.
- `must_not_claim`: `traffic_change`.
- Impact: lost afternoon ad revenue for ADV-3 and missed sales for L-03 after 10:00.
- Note: `advertiser_id` is a segment of the ads metrics only. The ads metrics have dimension `advertiser_id` in addition to the global dimensions. Add it to `Dimension` in T00.

## S2 Category sales fall, demand stable

Story: home category orders fall. Searches and clicks for home are stable.

Injection:
- `change_log`: `{system: "purchasing", kind: "release", planned: false, description: "purchasing v2.3: new demand forecast model"}`, 3 sim days before the inject time (backdated). The bug starts at that release: 1P purchase order `qty` for home SKUs × 0.3.
- Because of the 48 h lead time and 10 days of stock, the visible stockouts start about 2–3 days after the release. To make the demo fast, `inject` writes the backdated release and the reduced POs in history and sets 1P stock of H01–H06 to 0.5 days of sales at the inject time.

Decoys:
- SUP-4 has a delayed delivery for one low-demand toys SKU (T05) at the same time. Hypothesis "supplier delay" must be refuted for home.
- 3P sellers of home SKUs are fine; they still have stock. Their Buy Box share rises (symptom, not cause).

Expected:
- `entry_metrics`: `orders` `{category: home}`, `in_stock_rate` `{category: home}`.
- `root_node`: `po_fill_rate` or `po_qty_vs_forecast`; `cause_type`: `po_shortfall`; verdict `CONFIRMED`.
- `must_mention`: `purchasing v2.3`, `home`, `purchase order`, `out of stock`.
- `must_not_claim`: `supplier_delay`, `traffic_change`.

## S3 Search: searches steady, buyers fewer (DE)

Story: German search results become irrelevant after a ranker release.

Injection:
- `change_log`: `{system: "search-ranker", kind: "release", planned: false, description: "search-ranker v7.2", scope: {"country": "DE"}}`.
- Effect: `relevance[DE]` 0.85 → 0.45. Fewer result clicks; each click converts worse.

Decoys:
- A `search-ui v3.0` release in FR at the same hour, with no effect. Hypothesis "search-ui release" must be refuted (FR is normal).
- An `app-android` release 2 h earlier, with no effect.

Expected:
- `entry_metrics`: `search_conversion` `{country: DE}`, `orders` `{country: DE}`, `orders`.
- `root_node`: `search_relevance`; segment `{country: DE}`; `cause_type`: `release`; verdict `CONFIRMED`.
- `must_mention`: `DE`, `search-ranker`, `v7.2`, `relevance`.
- `must_not_claim`: `traffic_change`, `stockout`.
- The report must say that searches are stable (searches z ≈ 0) and all platforms in DE are affected (so not an app bug).

## S4 For You: clicks up, purchases down

Story: a new reco model prefers attractive items and drops the inventory filter.

Injection:
- `change_log`: `{system: "foryou-reco", kind: "model_deploy", planned: false, description: "foryou model v12 (engagement objective)"}`.
- Effect: `exposure_weight = attractiveness² × popularity` and no in-stock filter. Hyped out-of-stock SKUs (E11, F09, T07) and other out-of-stock SKUs get about 35% of impressions (baseline about 2%). Feed CTR rises; feed purchases fall.

Decoys:
- Feed impressions are stable (not a traffic change).
- A `catalog-svc` release the same day with no effect.

Expected:
- `entry_metrics`: `feed_purchase_rate`, `feed_ctr` (up), `orders` `{channel: feed}`.
- `root_node`: `feed_unavailable_exposure_share`; `cause_type`: `model_change`; verdict `CONFIRMED` or `HYPOTHESIS` with the model change as top hypothesis (the inventory-filter part needs SKU-level evidence).
- `must_mention`: `v12`, `out of stock` or `unavailable`, `exposure`.
- `must_not_claim`: `traffic_change`.
- Remaining uncertainty to report: whether the filter was removed on purpose or by mistake.

## S5 Orders up, contribution down

Story: marketing buys more traffic, the reco feed pushes platform-funded discounts, orders rise, contribution margin falls.

Injection:
- `change_log` (planned: true): `{system: "growth", kind: "campaign", description: "paid social push +200% (Cyber Week)", expected_effect: {"sessions": "+20%"}}`.
- `change_log` (planned: false): `{system: "promo-engine", kind: "config", description: "deals config 2026-11: platform funds 20% on home deals"}`.
- Effects: `marketing_mult` 1.25 with conversion of the extra sessions at 0.6× normal; marketing spend × 3; platform-funded 20% discount on home SKUs; feed exposure shifts +40% toward discounted home SKUs (lower take-rate mix vs electronics? no: home take rate is higher, but the discount is platform-funded).

Ground-truth contribution split (approximate, check with T04 test):
- marketing_cost increase ≈ 55% of the contribution drop
- platform_discounts ≈ 40%
- mix and other ≈ 5%

Decoys:
- `fee_revenue` rises (orders up). Hypothesis "fee change" must be refuted: take rates unchanged.

Expected:
- `entry_metrics`: `contribution_margin`, `contribution_per_order`.
- `root_node`: `marketing_cost` and `platform_discounts` (two causes); verdict `MIXED` or `CONFIRMED` with both in contributions.
- `must_mention`: `marketing`, `discount`, `promo-engine`, contribution shares with numbers.
- Contributions table: shares within ±10 pp of the ground truth.

## S6 Black Friday cascade (main demo)

Full story: `00-overview.md` §3. Parameters:

| Step | Rule |
|---|---|
| T0 | I1 effects: `campaign_mult` 1.4; platform-funded 10% coupons on electronics (`platform_discount`). `change_log` planned: `black-friday-2026`, `expected_effect: {"sessions": "+40%", "orders": "+35%", "platform_discounts_eur_per_day": 25000}` |
| T0+2h | 1P price −15% on E01–E10. `change_log` planned: `{system: "pricing-1p", kind: "price_change", description: "BF price cut top-10 electronics", expected_effect: {"1P electronics GMV": "+20%"}}` |
| T0+2h… | Repricers react by their rules (§4.2 in 03). No extra code. |
| T0+8h… | Small sellers that won the Buy Box at low prices sell out. Capacity limits block restock. |
| T0+10h… | Bidding rule (§4.5 in 03) raises bids; electronics CPC rises. |
| T0+12h | S-99 activates: offers on E02, E04, E06, E08 at 0.70 × ref_price, stock 500 each, refund probability 0.35. No `change_log` row (a new seller does not announce itself). `sellers.joined_ts` = T0+12h. |
| T0+60h… | Refunds on S-99 orders appear. |

The innocent part (I1) is in the same incident. The Skeptic must quantify it from `expected_effect`.

Expected (one merged story):
- `entry_metrics`: `small_seller_gmv_share`, `buybox_share` `{seller_tier: small_3P}`, `price_index_vs_1p`, `contribution_per_order`, `sellers_below_cost` (SellerOS only).
- verdict `MIXED`.
- `trigger`: hypothesis with `cause_type: price_change`, root `price_index_vs_1p`, first change at T0+2h.
- `loop`: contains `price_index_vs_1p` → `buybox_share` → `in_stock_rate` (and optionally → `avg_cpc`).
- Contributions include a `planned` row for the campaign.
- `affected_sellers` contains S-03, S-07, S-12 (no floor) — this needs SellerOS evidence.
- `must_mention`: `1P`, `price`, `repricer`, `small`, `Black Friday`, `S-99` (once the hijacker is active), `SellerOS`.
- `must_not_claim` as root cause: `traffic_change`, `planned_event`.
- Notification: SellerOS receives a `SellerAlert` for S-03, S-07, S-12.
- Open questions must include seller churn risk and refund trend.

Tuning targets at T0+24h (T05 test): orders total +25..40%; small_3P GMV share −25..−45%; electronics avg CPC +40..+80%; ≥ 3 small sellers out of stock on ≥ 1 SKU; S-99 Buy Box on ≥ 3 SKUs.

## S7 Black Friday checkout outage (front-end release)

Story: during Black Friday, a front-end release breaks the checkout button. Revenue drops suddenly. It is not obvious, because Black Friday traffic is high, many systems changed that day, and every back-end system looks healthy.

Injection (normally at I1 T0 + 3h):
- `change_log`: `{system: "checkout-web", kind: "release", planned: false, description: "checkout-web v5.8: new Place Order button component"}`.
- Effect: `checkout_ok[web] = 0.10`, `checkout_ok[android] = 0.10` (the Android app loads the `checkout-web` bundle in a webview). iOS is native → not affected.
- `client_errors_hourly`: new error `TypeError` on page `checkout`, `app_version = checkout-web v5.8`, count ≈ `carts × 0.8` per hour on web and android.
- Carts keep growing (Black Friday). Checkout starts, payment attempts, and orders fall on web and android.

Decoys (all on the same day):
- I1 Black Friday campaign (planned): sessions +40%. Totals of sessions and carts look great.
- `external_status`: `{provider: "PSP-2", status: "degraded", description: "elevated latency"}` 1 h before the release, for 2 h. PSP-2 `latency_p95_ms` rises, but `successes / attempts` stays normal. Hypothesis "payment provider failure" must be refuted: the success rate is normal, the attempts fell.
- `app-android v9.14` release rolling out (20%) the same morning. Hypothesis "android app release" must be refuted: web is affected too, and the affected android sessions do not depend on app version.
- Baseline noise releases in other systems.

How it shows in the metrics:
- `gmv` and `orders` total: about −35% to −45% against the Black Friday expectation, while sessions are +40%. Against a normal Friday, orders can look nearly flat, so a simple "orders fell vs last week" alert can miss it. The Skeptic's comparison with the planned `expected_effect` (orders +35%) shows the gap.
- `cart_to_checkout_rate` on web and android: −85%. iOS normal.
- `payment_success_rate`: normal.

Expected:
- `entry_metrics`: `gmv`, `orders`, `cart_to_checkout_rate`, `checkout_conversion`.
- `root_node`: `cart_to_checkout_rate`; segment `{platform: web}` and `{platform: android}`; `cause_type`: `release`; verdict `CONFIRMED`.
- Evidence groups: data (funnel step), change_log (`checkout-web v5.8` at the same hour), engineering (`client_errors_hourly` spike for that version on page `checkout`), service catalog (`app_components`: android checkout uses `checkout-web`).
- `must_mention`: `checkout-web`, `v5.8`, `web`, `android`, `iOS` (as the unaffected control), `PSP-2` (refuted), lost GMV per hour.
- `must_not_claim`: `traffic_change`, `planned_event`, and payment provider failure as root cause.
- Suggested action: roll back `checkout-web` to v5.7.
- Impact: lost GMV per hour = (expected Black Friday GMV − actual GMV) for web + android.

## I1 Innocent: Black Friday campaign only

Injection: only the T0 row of S6 (campaign + coupons). No 1P price cut, no hijacker.
Expected: sessions and orders rise in line with `expected_effect` (within ±25%). Verdict `INNOCENT`. No alert. An incident may be created with status `innocent` and shown in a separate "cleared" list.
`must_mention`: `black-friday-2026`, `planned`.

## I2 Missing data: FR late partition

Injection: for 3 sim hours from inject time, `orders_hourly` and `partition_status` for FR get `loaded_rows = 0.4 × expected_rows` (only 40% of FR order rows are written), `loaded_at = ts + 6h`. After 6 h the missing rows are backfilled (the true values are written).
Expected: the Sentinel defers (completeness < 0.9) or the Skeptic labels `DATA_ISSUE`. No business alert. After backfill the metric is normal, so the deferred item closes.
`must_mention` (if an incident exists): `FR`, `incomplete` or `late`, `partition`.

## Combined runs for the demo

| Run | Inject | Expected |
|---|---|---|
| D1 | I1 + S3 at the same hour | I1 cleared (`innocent`), S3 incident `CONFIRMED` |
| D2 | S6 | one merged `MIXED` story, SellerOS notified |
| D3 | S3, then `clear(S3)` | S3 incident → `resolved` after 3 normal ticks, no duplicate incident |
| D4 | S1, S2, S4, S5, I2 one by one | each matches its oracle |
| D5 | I1 at T0, S7 at T0+3h | I1 cleared; S7 incident `CONFIRMED` within 2 ticks, rollback suggested, PSP-2 refuted |
