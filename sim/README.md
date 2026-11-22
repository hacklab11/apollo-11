# Continuous simulator

Run `uv run eye-sim serve` from the repository root. It binds to
`127.0.0.1:8100` by default. This service never calls a model.

The simulator owns its DuckDB world. Empty worlds backfill the configured history;
existing worlds replay persisted scenario commands and seller actions to restore
mutable inventory, pricing and the clock without duplicating facts.
Startup lands all historical hours before accepting requests. Do not run two
writers against the same world or lake.

Compose runs this as `sim`, binding `0.0.0.0:8100` inside the container and
`127.0.0.1:8100` on the host. It uses a private
`sim-world` volume plus the shared `sim-lake` volume (`/app/data/lake`). Mount
`sim-lake` read-only in data consumers; never open the private world database
from another service. The data service consumes the live lake through `ParquetLanding`;
Core reads the data service over HTTP. Only one `sim` writer may use these
volumes. MCP's Compose host port starts at 8110, leaving 8100 for this service.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `SIM_HOST` | `127.0.0.1` | Bind address |
| `SIM_PORT` | `8100` | HTTP port |
| `SIM_WORLD_DB` | `data/world.duckdb` | Private world and replay journal |
| `SIM_LAKE_DIR` | `data/lake` | Public Parquet landing directory |
| `SIM_SECONDS_PER_SIM_HOUR` | `sim.seconds_per_sim_hour` in settings | Positive real-time interval between hours |
| `SIM_SCENARIOS` | `s8` | Comma-separated enabled plugins; empty disables all |

Unknown plugin names fail startup. Keep simulator settings and plugin configuration
unchanged when resuming a world: reconstruction is deterministic replay using
those settings, not a serialized in-memory checkpoint.

## HTTP

- `GET /health`: readiness; returns 503 if the background loop failed.
- `GET /status`: next sim hour, last landed hour, active scenarios.
- `GET /scenarios`: enabled plugin IDs and titles.
- `POST /scenarios/s8`: start the planned electronics coupon campaign now.
- `DELETE /scenarios/s8`: restore original seller prices/repricer modes and
  remove the campaign discounts; sold stock stays sold.

Unknown/disabled IDs return 404; duplicate triggers and clearing inactive plugins
return 409. Scenario changes and stepping share one lock and a dedicated worker
thread. IDs are case-insensitive; listings use lowercase IDs.

S8 gives electronics 10% platform-funded coupons for 24 simulated hours, without
increasing sessions. The seller typo begins `S8.typo_delay_hours` hours later
(`config/scenarios.yaml`, default 1). Selection, stock calibration, repricer
effects and the half-loaded first typo hour remain deterministic. The next tick
repairs that hour's missing orders. Ground truth stays in private `scenario_log`.

## Lake contract

Every public table in `eye.schema.SCHEMA` is written to:

```text
<lake>/<table>/hour=YYYY-MM-DDTHH/data.parquet
```

Tables with a `ts` column contain only that event hour. Tables without `ts`
(`skus`, `sellers`, `app_components`, `purchase_orders`,
`fba_capacity_daily`) contain snapshots: readers must select the latest
published snapshot, **not union their hourly copies**. This preserves mutable
purchase-order delivery state and daily capacity updates.

Each file is produced with DuckDB `COPY ... (FORMAT PARQUET)`, then atomically
replaced. `partition_status` is published last; its presence is the hour's
publication marker. Readers must still respect its completeness values. During
republishing the old marker remains available until its atomic replacement;
a failed re-land retains the previously published marker.
The preceding hour is republished after each step to expose repaired late orders.
Restart republishes history, recovering an interrupted landing. Files ending in
`.tmp` are not public data.

Neither `scenario_log`, `scenario_commands` nor `seller_actions` is landed.

The existing `generate`, `stats` and `export` commands remain available;
`generate --play` now accepts only S8.

## Offline checks

```bash
uv run pytest tests/sim -q
uv run ruff check . && uv run ruff format --check .
uv run lint-imports
uv run pytest -m "not live" -q
```

No live simulator stack or model calls are needed for verification.
