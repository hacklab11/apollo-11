"""DuckDB schema and atomic batch writes for simulator data."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Lock
from typing import Any

import duckdb

WORLD_DB = Path("data/world.duckdb")

_SCHEMA = {
    "skus": """sku VARCHAR, category VARCHAR, ref_price DOUBLE, popularity DOUBLE,
               attractiveness DOUBLE, supplier_id VARCHAR""",
    "sellers": "seller_id VARCHAR, tier VARCHAR, rating DOUBLE, joined_ts TIMESTAMPTZ",
    "traffic_hourly": """ts TIMESTAMPTZ, country VARCHAR, platform VARCHAR, sessions BIGINT,
                         search_sessions BIGINT, feed_sessions BIGINT, other_sessions BIGINT""",
    "search_hourly": """ts TIMESTAMPTZ, country VARCHAR, platform VARCHAR, category VARCHAR,
                        searches BIGINT, result_clicks BIGINT, relevance DOUBLE""",
    "feed_hourly": """ts TIMESTAMPTZ, country VARCHAR, platform VARCHAR, sku VARCHAR,
                      impressions BIGINT, clicks BIGINT, in_stock BOOLEAN""",
    "checkout_hourly": """ts TIMESTAMPTZ, country VARCHAR, platform VARCHAR, carts BIGINT,
                          checkout_starts BIGINT, payment_attempts BIGINT""",
    "payments_hourly": """ts TIMESTAMPTZ, country VARCHAR, platform VARCHAR, psp VARCHAR,
                          attempts BIGINT, successes BIGINT, latency_p95_ms DOUBLE""",
    "client_errors_hourly": """ts TIMESTAMPTZ, platform VARCHAR, app_version VARCHAR, page VARCHAR,
                               error_type VARCHAR, errors BIGINT""",
    "app_components": "platform VARCHAR, component VARCHAR, source VARCHAR, current_version VARCHAR",
    "external_status": "id VARCHAR, ts TIMESTAMPTZ, provider VARCHAR, status VARCHAR, description VARCHAR",
    "orders_hourly": """ts TIMESTAMPTZ, country VARCHAR, platform VARCHAR, category VARCHAR, sku VARCHAR,
                       seller_id VARCHAR, seller_tier VARCHAR, channel VARCHAR, units BIGINT,
                       gmv DOUBLE, fee_revenue DOUBLE, cogs_1p DOUBLE, platform_discount DOUBLE,
                       fulfillment_cost DOUBLE""",
    "refunds_hourly": """ts TIMESTAMPTZ, country VARCHAR, category VARCHAR, sku VARCHAR,
                        seller_id VARCHAR, seller_tier VARCHAR, refunds BIGINT, refunded_gmv DOUBLE,
                        refund_cost DOUBLE""",
    "offers_hourly": """ts TIMESTAMPTZ, sku VARCHAR, seller_id VARCHAR, seller_tier VARCHAR,
                       category VARCHAR, price DOUBLE, stock BIGINT, in_stock BOOLEAN, is_buybox BOOLEAN""",
    "purchase_orders": """po_id VARCHAR, sku VARCHAR, supplier_id VARCHAR, ordered_ts TIMESTAMPTZ,
                          qty BIGINT, eta_ts TIMESTAMPTZ, delivered_ts TIMESTAMPTZ,
                          delivered_qty BIGINT, system_version VARCHAR""",
    "fba_capacity_daily": "day DATE, seller_id VARCHAR, capacity_units BIGINT, used_units BIGINT, blocked_units BIGINT",
    "ads_hourly": """ts TIMESTAMPTZ, advertiser_id VARCHAR, country VARCHAR, seller_id VARCHAR, category VARCHAR,
                    impressions BIGINT, clicks BIGINT, spend DOUBLE, avg_cpc DOUBLE, bid DOUBLE,
                    daily_budget DOUBLE, spent_today DOUBLE, planned_cum_share DOUBLE""",
    "marketing_hourly": "ts TIMESTAMPTZ, country VARCHAR, channel VARCHAR, spend DOUBLE, attributed_sessions BIGINT",
    "change_log": """id VARCHAR, ts TIMESTAMPTZ, system VARCHAR, kind VARCHAR, planned BOOLEAN,
                     scope JSON, description VARCHAR, expected_effect JSON""",
    "partition_status": """table_name VARCHAR, country VARCHAR, ts TIMESTAMPTZ, expected_rows BIGINT,
                           loaded_rows BIGINT, loaded_at TIMESTAMPTZ""",
    "scenario_log": "id VARCHAR, ts TIMESTAMPTZ, scenario_id VARCHAR, action VARCHAR, params JSON",
}
TABLES = tuple(_SCHEMA)

type Row = tuple[Any, ...]
type Batches = dict[str, list[Row]]


class Store:
    def __init__(self, path: str | Path = WORLD_DB) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._con = duckdb.connect(str(path))
        self._con.execute("SET TimeZone = 'UTC'")
        self._con.execute(
            ";".join(
                f"CREATE TABLE IF NOT EXISTS {table} ({columns})"
                for table, columns in _SCHEMA.items()
            )
        )

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self._con.cursor()

    def append(self, batches: Mapping[str, Sequence[Row]]) -> None:
        """Publish a whole tick at once; readers never see a partial hour."""
        if unknown := batches.keys() - _SCHEMA.keys():
            raise ValueError(f"unknown tables: {', '.join(sorted(unknown))}")
        with self._lock:
            self._con.begin()
            try:
                for table, rows in batches.items():
                    if not rows:
                        continue
                    columns = [list(column) for column in zip(*rows, strict=True)]
                    values = ", ".join("unnest(?)" for _ in columns)
                    self._con.execute(f"INSERT INTO {table} SELECT {values}", columns)
                self._con.commit()
            except BaseException:
                self._con.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._con.close()
