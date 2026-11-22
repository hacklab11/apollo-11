# Simulator

Entrypoint: `eye-sim serve`; implementation: `sim/src/eye/sim/`.
The service owns the private DuckDB world, advances one simulated hour per configured interval,
and publishes a public Parquet lake. `SIM_WORLD_DB`, `SIM_LAKE_DIR`, `SIM_HOST`, `SIM_PORT`
and `SIM_SECONDS_PER_SIM_HOUR` configure the service; the interval otherwise comes from settings.

`lake.py` writes hourly `table/hour=YYYY-MM-DDTHH/data.parquet` files and publishes
`partition_status` last as the manifest. The previous hour is republished on the next tick,
allowing S8's intentionally partial first hour to be repaired. Agents cannot read private
`scenario_log`; scenario controls/status are for demo/test operation, not investigative evidence.

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
