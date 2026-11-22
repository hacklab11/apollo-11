"""Warehouse contract shared by ingestion sources and readers."""

import duckdb

SCHEMA = {
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
}
TABLES = tuple(SCHEMA)


def create_tables(con: duckdb.DuckDBPyConnection) -> None:
    for table, columns in SCHEMA.items():
        con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")


def validate_schema(con: duckdb.DuckDBPyConnection) -> None:
    catalog = con.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_catalog = current_database() AND table_schema = 'main'"
    ).fetchall()
    actual: dict[str, dict[str, str]] = {}
    for table, column, dtype in catalog:
        actual.setdefault(table, {})[column] = dtype
    errors = []
    for table, columns in SCHEMA.items():
        if table not in actual:
            errors.append(f"missing table {table}")
            continue
        for definition in columns.split(","):
            column, dtype = definition.split()
            expected = "TIMESTAMP WITH TIME ZONE" if dtype == "TIMESTAMPTZ" else dtype
            found = actual[table].get(column)
            if found is None:
                errors.append(f"missing column {table}.{column}")
            elif found != expected:
                errors.append(
                    f"mistyped column {table}.{column}: expected {expected}, got {found}"
                )
    if errors:
        raise ValueError("Invalid warehouse schema: " + "; ".join(errors))
