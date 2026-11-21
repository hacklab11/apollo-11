"""Read-only specialist domain queries over simulator data."""

import json
from collections.abc import Callable
from datetime import date, timedelta
from typing import Annotated, Any, Literal, Self

import duckdb
from fastapi import APIRouter, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from eye.contracts import Category, Country, Platform, SellerTier


class DataParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: AwareDatetime
    end: AwareDatetime
    advertiser_id: str | None = Field(None, min_length=1, max_length=100)
    day: date | None = None
    category: Category | None = None
    country: Country | None = None
    platform: Platform | None = None
    sku: str | None = Field(None, min_length=1, max_length=100)
    tier: SellerTier | None = None
    since: AwareDatetime | None = None
    top_n: int = Field(20, ge=1, le=5000)
    psp: Literal["PSP-1", "PSP-2"] | None = None
    page: str | None = Field(None, min_length=1, max_length=100)
    system: str | None = Field(None, min_length=1, max_length=100)
    kind: str | None = Field(None, min_length=1, max_length=100)
    name: str | None = Field(None, pattern=r"^[a-z][a-z0-9_]{2,40}$")
    segment: str = "{}"

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if not timedelta(0) < self.end - self.start <= timedelta(days=14):
            raise ValueError("require start < end and window <= 14 days")
        try:
            segment = json.loads(self.segment)
        except ValueError:
            raise ValueError("segment must be a JSON object") from None
        if not isinstance(segment, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in segment.items()
        ):
            raise ValueError("segment must map dimension names to strings")
        return self


class DataResponse(BaseModel):
    rows: list[dict[str, Any]] = Field(max_length=5000)
    summary: str = Field(max_length=300)


# Every query exposes a ts column for a common, half-open time filter.
_QUERIES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "ads/pacing": (
        (
            "SELECT *, daily_budget * planned_cum_share AS planned_spend, "
            "impressions > 0 AS participating FROM ads_hourly"
        ),
        ("advertiser_id",),
        "SELECT * FROM filtered ORDER BY ts, advertiser_id",
    ),
    "ads/auction": (
        (
            "SELECT coalesce(a.ts,s.ts) AS ts, coalesce(a.category,s.category) AS category, "
            "a.* EXCLUDE (ts,category), coalesce(s.slots,0) AS available_slots "
            "FROM (SELECT ts, category, advertiser_id, sum(impressions) AS impressions, "
            "sum(clicks) AS clicks, sum(spend) AS spend, avg(bid) AS bid "
            "FROM ads_hourly GROUP BY ts, category, advertiser_id) a FULL OUTER JOIN "
            "(SELECT ts, category, 2*sum(searches) AS slots FROM search_hourly "
            "GROUP BY ts, category) s ON a.ts=s.ts AND a.category=s.category"
        ),
        ("category",),
        (
            "SELECT ts, category, count(DISTINCT advertiser_id) AS bidders, "
            "avg(bid) AS avg_bid, sum(spend)/nullif(sum(clicks),0) AS cpc, "
            "max(available_slots) AS slots, coalesce(sum(impressions),0) AS filled_slots, "
            "coalesce(sum(impressions),0)::DOUBLE/nullif(max(available_slots),0) AS slot_fill_rate, "
            "list_slice(list(advertiser_id ORDER BY spend DESC, "
            "advertiser_id) FILTER (WHERE advertiser_id IS NOT NULL),1,10) AS top_advertisers "
            "FROM filtered GROUP BY ts, category ORDER BY ts, category"
        ),
    ),
    "marketing": (
        "SELECT * FROM marketing_hourly",
        ("country",),
        (
            "SELECT ts, country, channel, sum(spend) AS spend, "
            "sum(attributed_sessions) AS attributed_sessions FROM filtered "
            "GROUP BY ts, country, channel ORDER BY ts, country, channel"
        ),
    ),
    "discounts": (
        "SELECT * FROM orders_hourly",
        ("category",),
        (
            "SELECT ts, category, sku, sum(platform_discount) AS platform_discount "
            "FROM filtered GROUP BY ts, category, sku ORDER BY ts, category, sku"
        ),
    ),
    "offers": (
        "SELECT * FROM offers_hourly",
        ("sku", "category"),
        "SELECT * FROM filtered ORDER BY ts, sku, seller_id",
    ),
    "buybox": (
        "SELECT * FROM offers_hourly",
        ("category",),
        (
            "SELECT seller_id, seller_tier, category, sum(is_buybox::INT) AS wins, "
            "sum(is_buybox::INT)::DOUBLE / nullif(sum(sum(is_buybox::INT)) OVER "
            "(PARTITION BY category),0) AS buybox_share "
            "FROM filtered GROUP BY seller_id, seller_tier, category "
            "ORDER BY category, seller_id"
        ),
    ),
    "stock": (
        (
            "SELECT ts, category, sku, bool_or(in_stock) AS in_stock "
            "FROM offers_hourly GROUP BY ts, category, sku"
        ),
        ("category",),
        (
            "SELECT category, count(DISTINCT sku) FILTER (WHERE in_stock) AS in_stock_skus, "
            "count(DISTINCT sku) FILTER (WHERE NOT in_stock) AS stockout_skus, "
            "count(*) FILTER (WHERE NOT in_stock) AS hours_out_of_stock "
            "FROM filtered GROUP BY category ORDER BY category"
        ),
    ),
    "purchase_orders": (
        (
            "SELECT p.*, p.ordered_ts AS ts, s.category FROM purchase_orders p "
            "JOIN skus s USING (sku)"
        ),
        ("category",),
        "SELECT * FROM filtered ORDER BY ts, po_id",
    ),
    "capacity": (
        (
            "SELECT c.*, c.day::TIMESTAMPTZ AS ts, s.tier FROM fba_capacity_daily c "
            "JOIN sellers s USING (seller_id)"
        ),
        ("tier",),
        "SELECT * FROM filtered ORDER BY ts, seller_id",
    ),
    "sellers/new": (
        (
            "SELECT s.*, s.joined_ts AS ts, o.ts AS offer_ts, o.sku, o.price, o.stock, "
            "o.is_buybox FROM sellers s LEFT JOIN offers_hourly o ON "
            "s.seller_id = o.seller_id AND o.ts >= ? AND o.ts < ?"
        ),
        (),
        "SELECT * FROM filtered ORDER BY ts, seller_id, offer_ts, sku",
    ),
    "search/quality": (
        (
            "SELECT s.*, coalesce(o.units,0) AS units FROM search_hourly s LEFT JOIN "
            "(SELECT ts, country, platform, category, sum(units) AS units "
            "FROM orders_hourly WHERE channel='search' GROUP BY ALL) o "
            "USING (ts,country,platform,category)"
        ),
        ("country",),
        (
            "SELECT country, category, sum(relevance*searches)/nullif(sum(searches),0) "
            "AS relevance, sum(result_clicks)::DOUBLE/nullif(sum(searches),0) AS ctr, "
            "sum(units)::DOUBLE/nullif(sum(searches),0) AS conversion "
            "FROM filtered GROUP BY country, category ORDER BY country, category"
        ),
    ),
    "feed/exposure": (
        "SELECT f.*, s.attractiveness FROM feed_hourly f JOIN skus s USING (sku)",
        (),
        (
            "SELECT sku, sum(impressions) AS impressions, sum(clicks) AS clicks, "
            "bool_and(in_stock) AS in_stock, max(attractiveness) AS attractiveness "
            "FROM filtered GROUP BY sku ORDER BY impressions DESC, sku"
        ),
    ),
    "checkout/funnel": (
        (
            "SELECT c.*, coalesce(p.successes,0) AS successes FROM checkout_hourly c "
            "LEFT JOIN (SELECT ts,country,platform,sum(successes) AS successes "
            "FROM payments_hourly GROUP BY ALL) p USING (ts,country,platform)"
        ),
        ("country", "platform"),
        (
            "SELECT ts, sum(carts) AS carts, sum(checkout_starts) AS checkout_starts, "
            "sum(payment_attempts) AS payment_attempts, sum(successes) AS successes "
            "FROM filtered GROUP BY ts ORDER BY ts"
        ),
    ),
    "payments": (
        "SELECT * FROM payments_hourly",
        ("psp",),
        (
            "SELECT ts, psp, sum(attempts) AS attempts, sum(successes) AS successes, "
            "sum(successes)::DOUBLE/nullif(sum(attempts),0) AS success_rate, "
            "max(latency_p95_ms) AS latency_p95_ms "
            "FROM filtered GROUP BY ts, psp ORDER BY ts, psp"
        ),
    ),
    "errors": (
        "SELECT * FROM client_errors_hourly",
        ("platform", "page"),
        (
            "SELECT platform, app_version, page, error_type, sum(errors) AS errors "
            "FROM filtered GROUP BY ALL ORDER BY platform, app_version, page, error_type"
        ),
    ),
    "app_components": (
        "SELECT * FROM app_components",
        (),
        "SELECT * FROM filtered ORDER BY platform, component",
    ),
    "changes": (
        (
            "SELECT ts, id, system, kind, planned, scope, description, expected_effect, "
            "NULL AS status "
            "FROM change_log UNION ALL SELECT ts, id, provider AS system, "
            "'external_status' AS kind, false AS planned, NULL AS scope, description, "
            "NULL AS expected_effect, status FROM external_status"
        ),
        ("system", "kind"),
        "SELECT * FROM filtered ORDER BY ts, id",
    ),
}

type MetricTool = Callable[..., BaseModel | list[BaseModel] | dict[str, Any]]


def create_router(
    connection: duckdb.DuckDBPyConnection,
    *,
    query_metric: MetricTool | None = None,
    baseline_compare: MetricTool | None = None,
) -> APIRouter:
    """Build a router using a shared connection; each request owns its cursor."""
    router = APIRouter(prefix="/data", tags=["data"])

    def endpoint(path: str) -> Callable[..., DataResponse]:
        def read(params: Annotated[DataParams, Query()]) -> DataResponse:
            source, filters, result = _QUERIES[path]
            where, values = [], []
            if path == "sellers/new":
                values.extend([params.start, params.end])
            if path != "app_components":
                where.extend(["ts >= ?", "ts < ?"])
                start = (
                    params.since or params.start
                    if path == "sellers/new"
                    else params.start
                )
                values.extend([start, params.end])
            for column in filters:
                value = getattr(params, column)
                if value is not None:
                    where.append(f"{column} = ?")
                    values.append(value)
            if path == "ads/pacing" and params.day is not None:
                where.append("CAST(ts AT TIME ZONE 'UTC' AS DATE) = ?")
                values.append(params.day)
            limit = params.top_n if path == "feed/exposure" else 5000
            sql = f"WITH source AS ({source}), filtered AS (SELECT * FROM source"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += f") {result} LIMIT ?"
            with connection.cursor() as cursor:
                cursor.execute(sql, [*values, limit])
                columns = [column[0] for column in cursor.description]
                rows = [
                    dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
                ]
            return DataResponse(
                rows=rows, summary=f"{path}: {len(rows)} rows (limit {limit})."
            )

        return read

    for path in _QUERIES:
        router.add_api_route(f"/{path}", endpoint(path), methods=["GET"], name=path)

    @router.get("/metric")
    def metric(params: Annotated[DataParams, Query()]) -> DataResponse:
        if params.name is None:
            raise HTTPException(422, "name is required")
        if query_metric is None or baseline_compare is None:
            raise HTTPException(503, "metric tools are not configured")
        arguments = {
            "name": params.name,
            "start": params.start,
            "end": params.end,
            "segment": json.loads(params.segment),
        }
        try:
            rows = [
                {
                    "query_metric": query_metric(**arguments),
                    "baseline_compare": baseline_compare(**arguments),
                }
            ]
            return DataResponse(
                rows=rows, summary=f"{params.name}: 1 metric comparison."
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    return router
