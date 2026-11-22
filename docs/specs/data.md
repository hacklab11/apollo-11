# Data service, registry and graph

Entrypoint: `eye-data`; implementation: `packages/data/src/eye/`.
The service binds to port 8150 by default and exposes `/healthz`, typed `/v1/*` operations,
and domain queries. `warehouse/operations.py` is the typed operation catalog;
`warehouse/server.py` binds it to FastAPI. `DataClient` implements `DataQueries.read()`
over HTTP; `LocalQueries` binds the same operations to a local database.

## Storage and computation

`warehouse/landing.py:ParquetLanding` imports manifest-published lake hours into DuckDB.
It throttles refresh to one second, transactionally replaces changed hours, and updates
non-hourly snapshots from the latest complete hour. Complete means every partition reports
`loaded_rows >= expected_rows`. A repaired manifest causes a refresh, not duplicate rows.

`config/metrics.yaml` defines SQL/formula metrics, descriptions, units, dimensions and status.
Ratios pool numerator/denominator rather than averaging row ratios. Pending metrics are not scanned.
`config/identities.yaml` defines algebraic identities; `config/causal_seeds.yaml` seeds causal candidates.
The registry engine evaluates the allowed formula AST; SQL guard rejects mutating/unsafe queries.

`graph/store.py` owns directed graph reads, paths, cycles and persistence.
`graph/bootstrap.py` derives formula/identity structure and validates seed evidence.
A causal candidate is not an algebraic identity, and a breakdown self-loop is not a causal cycle.
[Cartographer](cartographer.md) proposes and validates graph refinements.
See [tools](tools.md) for implemented numerical algorithms.

## Integration boundary

In Compose, Core and agents use `EYE_DATA_URL` and never open simulator world state.
Replace ingestion with schema-compatible Parquet and `partition_status` manifests under
`SIM_LAKE_DIR`, or serve the operation catalog from a real warehouse adapter and change
`EYE_DATA_URL`. Retain freshness/completeness semantics and keep `scenario_log` inaccessible.

Embedded Core (only when `EYE_DATA_URL` is absent) uses
`packages/orchestrator/src/eye/datasource.py:DataSource`: `now`, `history_start`,
`live_start`, `step()`, `cursor()`, `close()`. `data.source: sim` selects simulation;
`data.source: duckdb` and `data.path` select `DuckDBSource`.
A custom source needs a corresponding `open_source()` branch. These are YAML settings,
not generic environment overrides. Compose ingestion is a separate boundary.
