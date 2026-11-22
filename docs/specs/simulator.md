# Simulator

Entrypoint: `eye-sim serve`; implementation: `sim/src/eye/sim/`.
The service owns the private DuckDB world, advances one simulated hour per configured interval,
and publishes a public Parquet lake. `SIM_WORLD_DB`, `SIM_LAKE_DIR`, `SIM_HOST`, `SIM_PORT`
and `SIM_SECONDS_PER_SIM_HOUR` configure the service; the interval otherwise comes from settings.

`lake.py` writes hourly `table/hour=YYYY-MM-DDTHH/data.parquet` files and publishes
`partition_status` last as the manifest. The previous hour is republished on the next tick,
allowing S8's intentionally partial first hour to be repaired. Agents cannot read private
`scenario_log`; scenario controls/status are for demo/test operation, not investigative evidence.

## World and numerical model

The seeded catalog has 40 SKUs (12 electronics, ten home, ten fashion, eight toys),
one first-party seller, five large and 24 small third-party sellers, plus a dormant seller.
`world.py` defines category shares (35/25/25/15%), popularity, offer eligibility and private
seller attributes. `engine.py` uses seeded per-hour generators so the same settings and commands
reproduce the world. Each step writes `[now, now+1h)` and advances the clock by one hour.

- **Demand** (`demand.py`): 20,000 peak-hour sessions × country share (DE/FR 60/40) ×
  platform share (Android/iOS/web 45/35/20) × hourly/weekday profiles × modifiers ×
  lognormal noise (sigma 0.05). Search/feed/other split is 55/25/20%. The hour profile peaks
  at 20:00 (1.0), falls to 0.08 at 03:00–04:00; weekday factors are
  `1.00, 0.98, 0.97, 1.00, 1.05, 1.15, 1.20`.
- **Funnel**: search clicks use `0.45 × relevance/0.85`; feed uses 12 impressions/session,
  click probability `0.04 × attractiveness`, and an unavailable-item weight of 0.05.
  Cart probability is `0.12 × clip((reference/price)^1.5, 0.5, 2) × relevance_factor`;
  checkout/payment-attempt probabilities are 0.75/0.90. PSP split is 60/40 with baseline
  success 0.97/0.96. Unavailable clicks retry another SKU with probability 0.30.
- **Buy Box and repricing** (`market.py`): lowest `price × (1 + 0.5 × (1-rating))` wins;
  ties prefer rating then seller ID. Match/beat react to a price gap over 3%; beat undercuts
  by 1%. Recovery when competitors are over 3% dearer targets 99% of their price, capped at
  reference. Prices below 90% of reference drift up 5% when no cheaper competitor blocks it.
  Tie-beating also requires a competitor below 97% of reference, preventing a baseline price war.
  Missing floors use 40% of reference; fixed/manual overrides do not reprice.
- **Supply**: initial stock is ten baseline days for 1P, three for 3P. Third-party restock
  targets seven days when coverage falls below two, with 48-hour transit and capacity limits.
  Weekly capacity is three baseline weeks for large sellers, 1.3 for small sellers.
  First-party purchase orders target ten days of seven-day-average sales, ETA 48h ±6h.
- **Ads** (`ads.py`): two sponsored slots, 90% impression fill, 2% CTR; next-bid CPC plus
  EUR 0.01 with EUR 0.10 reserve. Participation probability is
  `clip(1 - (spent/budget - planned_cumulative_share) × 5, 0, 1)` until budget exhaustion.
  Out-of-stock bidding can rise 25% per six hours, capped at 2.5× base.
- **Finance** (`finance.py`): GMV is units × price; third-party fee rates are 8/15/17/15%
  for electronics/home/fashion/toys. First-party margin subtracts reference-price COGS;
  fulfillment is EUR 5 per 1P unit. Baseline refund probability is 3%, delayed 48–96h;
  refund cost is 25% of refunded GMV.

`contribution_margin = fee_revenue + ad_revenue + first_party_margin - platform_discounts
- fulfillment_cost - marketing_cost - refund_cost`. The registry computes this identity;
the model must not estimate it from prose. No historical non-S8 trigger is implied by these mechanics.

## S8 and plugins

`scenarios/__init__.py` registers only `s8`; `SIM_SCENARIOS` selects registered plugins,
and unknown names fail rather than enabling unimplemented scenarios.
`config/scenarios.yaml` supplies S8 parameters. Adding another scenario requires an implemented
plugin and registry entry, not just changing the environment variable.

S8 combines a 24-hour planned electronics coupon campaign (10% of reference-price GMV)
with a delayed manual seller price typo. Seller selection is deterministic from eligible small
third-party electronics offers. The campaign is visible in `change_log`; actual price history,
Buy Box and seller intent are the evidence for the typo. Injection/clear do not rewind the world.

HTTP: `GET /health`, `/status`, `/scenarios`, `POST /scenarios/{id}` to inject and
`DELETE /scenarios/{id}` to clear. Unknown/disabled scenarios return 404; conflicting state changes return 409.
See [demo](demo.md) for the host command and [data](data.md) for real ingestion.
