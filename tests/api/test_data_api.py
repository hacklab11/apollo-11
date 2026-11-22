from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eye.sim.store import Store
from eye.warehouse.domain import DataResponse, create_router

START = "2026-11-27T00:00:00Z"
END = "2026-11-27T01:00:00Z"
WINDOW = {"start": START, "end": END}
PATHS = [
    "ads/pacing",
    "ads/auction",
    "marketing",
    "discounts",
    "offers",
    "buybox",
    "stock",
    "purchase_orders",
    "capacity",
    "sellers/new",
    "search/quality",
    "feed/exposure",
    "checkout/funnel",
    "payments",
    "errors",
    "app_components",
    "changes",
    "metric",
]


@pytest.fixture
def connection() -> Iterator[duckdb.DuckDBPyConnection]:
    store = Store(":memory:")
    with store.cursor() as con:
        # T03's tables exist in Store, but its generators are not merged yet.
        con.execute("""
            INSERT INTO skus VALUES ('E01','electronics',100,1,0.8,'SUP-1');
            INSERT INTO sellers VALUES ('S-01','small_3P',4.5,'2026-11-27');
            INSERT INTO offers_hourly VALUES ('2026-11-27','E01','S-01','small_3P','electronics',90,10,true,true);
            INSERT INTO ads_hourly VALUES ('2026-11-27','ADV-1','DE','S-01','electronics',100,10,20,2,2,100,20,0.3);
            INSERT INTO marketing_hourly VALUES ('2026-11-27','DE','paid_social',25,50);
            INSERT INTO orders_hourly VALUES ('2026-11-27','DE','web','electronics','E01','S-01','small_3P','search',3,270,20,0,5,1);
            INSERT INTO purchase_orders VALUES ('PO-1','E01','SUP-1','2026-11-27',10,'2026-11-28',NULL,0,'v1');
            INSERT INTO fba_capacity_daily VALUES ('2026-11-27','S-01',100,20,5);
            INSERT INTO search_hourly VALUES ('2026-11-27','DE','web','electronics',100,20,0.8);
            INSERT INTO feed_hourly VALUES ('2026-11-27','DE','web','E01',100,10,true);
            INSERT INTO checkout_hourly VALUES ('2026-11-27','DE','web',10,8,5);
            INSERT INTO payments_hourly VALUES ('2026-11-27','DE','web','PSP-1',5,3,200);
            INSERT INTO client_errors_hourly VALUES ('2026-11-27','web','v1','checkout','TypeError',2);
            INSERT INTO app_components VALUES ('web','checkout','checkout-web','v1');
            INSERT INTO change_log VALUES ('chg_1','2026-11-27','checkout','release',false,'{}','deploy','{}');
            INSERT INTO external_status VALUES ('ext_1','2026-11-27','PSP-1','ok','healthy');
        """)
        yield con
    store.close()


def make_client(connection: duckdb.DuckDBPyConnection, **tools: Any) -> TestClient:
    app = FastAPI()
    app.include_router(create_router(connection, **tools))
    return TestClient(app)


@pytest.fixture
def client(connection: duckdb.DuckDBPyConnection) -> Iterator[TestClient]:
    with make_client(
        connection,
        query_metric=lambda **kw: {"value": 3},
        baseline_compare=lambda **kw: {"expected": 3},
    ) as client:
        yield client


@pytest.mark.parametrize("path", PATHS)
def test_baseline_endpoints(client: TestClient, path: str) -> None:
    response = client.get(f"/data/{path}", params={**WINDOW, "name": "orders"})
    assert response.status_code == 200, response.text
    result = DataResponse.model_validate(response.json())
    assert result.rows
    assert str(len(result.rows)) in result.summary


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "bad",
    [
        {"start": "invalid"},
        {"start": END},
        {"end": "2026-12-12T00:00:00Z"},
        {"country": "US"},
        {"category": "unknown"},
        {"platform": "desktop"},
        {"tier": "3P"},
        {"psp": "unknown"},
        {"top_n": "0"},
        {"top_n": "5001"},
        {"day": "not-a-date"},
        {"since": "yesterday"},
        {"segment": "[]"},
        {"start": "2026-11-27T00:00:00"},
    ],
)
def test_bad_params(client: TestClient, path: str, bad: dict[str, str]) -> None:
    response = client.get(f"/data/{path}", params={**WINDOW, "name": "orders", **bad})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "path", [p for p in PATHS if p not in ("metric", "app_components")]
)
def test_empty_window(client: TestClient, path: str) -> None:
    response = client.get(
        f"/data/{path}",
        params={"start": "2026-11-28T00:00:00Z", "end": "2026-11-28T01:00:00Z"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == []
    assert "0 rows" in response.json()["summary"]


def test_aggregates_and_filters(client: TestClient) -> None:
    def rows(path: str, **params: str) -> list[dict[str, Any]]:
        return client.get(f"/data/{path}", params={**WINDOW, **params}).json()["rows"]

    assert rows("ads/pacing", advertiser_id="ADV-1")[0]["planned_spend"] == 30
    assert rows("ads/pacing", day="2026-11-28") == []
    assert rows("ads/auction")[0]["cpc"] == 2
    assert rows("buybox")[0]["buybox_share"] == 1
    assert rows("stock")[0]["in_stock_skus"] == 1
    assert rows("stock")[0]["hours_out_of_stock"] == 0
    assert rows("search/quality")[0]["ctr"] == 0.2
    assert rows("search/quality")[0]["conversion"] == 0.03
    assert rows("checkout/funnel")[0]["successes"] == 3
    assert rows("payments")[0]["success_rate"] == 0.6
    assert rows("errors", page="checkout")[0]["errors"] == 2
    assert rows("offers", sku="' OR 1=1 --") == []
    assert rows("offers", category="home") == []
    assert rows("marketing", country="FR") == []
    assert rows("capacity", tier="1P") == []
    assert rows("sellers/new", since=END) == []
    assert len(rows("changes")) == 2
    assert len(rows("changes", kind="external_status")) == 1


def test_limits_and_half_open_window(
    connection: duckdb.DuckDBPyConnection, client: TestClient
) -> None:
    connection.execute(
        "INSERT INTO offers_hourly SELECT o.* FROM offers_hourly o, range(6000)"
    )
    result = client.get("/data/offers", params=WINDOW)
    assert result.status_code == 200, result.text
    assert len(result.json()["rows"]) == 5000
    connection.execute(
        "INSERT INTO feed_hourly VALUES ('2026-11-27','DE','web','E02',200,20,false)"
    )
    connection.execute(
        "INSERT INTO skus VALUES ('E02','electronics',100,1,0.9,'SUP-1')"
    )
    assert (
        client.get("/data/feed/exposure", params={**WINDOW, "top_n": 1}).json()["rows"][
            0
        ]["sku"]
        == "E02"
    )
    assert (
        client.get(
            "/data/offers", params={"start": "2026-11-26T23:00:00Z", "end": START}
        ).json()["rows"]
        == []
    )
    ts = datetime(2026, 11, 27, tzinfo=UTC)
    assert (
        client.get(
            "/data/offers",
            params={
                "start": ts.isoformat(),
                "end": (ts + timedelta(days=14)).isoformat(),
            },
        ).status_code
        == 200
    )


def test_metric_proxy(connection: duckdb.DuckDBPyConnection) -> None:
    calls: list[dict[str, Any]] = []

    def tool(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"summary": "0 points", "points": []}

    with make_client(connection, query_metric=tool, baseline_compare=tool) as client:
        response = client.get(
            "/data/metric",
            params={**WINDOW, "name": "orders", "segment": '{"country":"DE"}'},
        )
        assert response.status_code == 200, response.text
        assert len(calls) == 2 and calls[0] == calls[1]
        assert calls[0]["segment"] == {"country": "DE"}
        assert calls[0]["start"].tzinfo is not None
        assert client.get("/data/metric", params=WINDOW).status_code == 422

    def invalid(**kwargs: Any) -> dict[str, Any]:
        raise ValueError("unknown metric")

    with make_client(connection, query_metric=invalid, baseline_compare=tool) as client:
        assert (
            client.get("/data/metric", params={**WINDOW, "name": "unknown"}).status_code
            == 422
        )
    with make_client(connection) as client:
        assert (
            client.get("/data/metric", params={**WINDOW, "name": "orders"}).status_code
            == 503
        )


def test_empty_static_table(
    connection: duckdb.DuckDBPyConnection, client: TestClient
) -> None:
    connection.execute("DELETE FROM app_components")
    assert client.get("/data/app_components", params=WINDOW).json()["rows"] == []


def test_new_seller_without_offers_in_window(
    connection: duckdb.DuckDBPyConnection, client: TestClient
) -> None:
    connection.execute("UPDATE offers_hourly SET ts = ts + INTERVAL '1 day'")
    response = client.get("/data/sellers/new", params=WINDOW)
    assert response.status_code == 200, response.text
    assert response.json()["rows"][0]["seller_id"] == "S-01"
    assert response.json()["rows"][0]["offer_ts"] is None


def test_new_sellers_since_before_window_start(
    connection: duckdb.DuckDBPyConnection, client: TestClient
) -> None:
    connection.execute("""
        INSERT INTO sellers VALUES
            ('S-02','small_3P',4.5,'2026-11-26'),
            ('S-03','small_3P',4.5,'2026-11-25'),
            ('S-04','small_3P',4.5,'2026-11-24'),
            ('S-05','small_3P',4.5,'2026-11-27 01:00:00');
        INSERT INTO offers_hourly
        SELECT ts, sku, 'S-02', seller_tier, category, price, stock, in_stock, is_buybox
        FROM offers_hourly;
        INSERT INTO offers_hourly
        SELECT ts - INTERVAL '1 day', sku, seller_id, seller_tier, category,
               price, stock, in_stock, is_buybox
        FROM offers_hourly WHERE seller_id='S-02';
    """)
    response = client.get(
        "/data/sellers/new", params={**WINDOW, "since": "2026-11-25T00:00:00Z"}
    )
    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    assert [row["seller_id"] for row in rows] == ["S-03", "S-02", "S-01"]
    assert rows[0]["offer_ts"] is None
    assert rows[1]["sku"] == "E01"
    assert rows[1]["offer_ts"] == rows[2]["offer_ts"]
    assert [
        row["seller_id"]
        for row in client.get("/data/sellers/new", params=WINDOW).json()["rows"]
    ] == ["S-01"]


def test_auction_slot_capacity(
    connection: duckdb.DuckDBPyConnection, client: TestClient
) -> None:
    def auction() -> dict[str, Any]:
        response = client.get("/data/ads/auction", params=WINDOW)
        assert response.status_code == 200, response.text
        return response.json()["rows"][0]

    row = auction()
    assert row["slots"] == 200
    assert row["filled_slots"] == 100
    assert row["slot_fill_rate"] == 0.5
    connection.execute("DELETE FROM ads_hourly")
    row = auction()
    assert row["slots"] == 200
    assert row["filled_slots"] == 0
    assert row["slot_fill_rate"] == 0
    assert row["bidders"] == 0
    connection.execute("UPDATE search_hourly SET searches=0")
    assert auction()["slot_fill_rate"] is None


def test_auction_ranks_advertisers_by_combined_country_spend(
    connection: duckdb.DuckDBPyConnection, client: TestClient
) -> None:
    connection.execute("""
        INSERT INTO ads_hourly
        SELECT ts, advertiser_id, 'FR', seller_id, category, impressions, clicks,
               15, avg_cpc, bid, daily_budget, spent_today, planned_cum_share
        FROM ads_hourly;
        INSERT INTO ads_hourly
        SELECT ts, 'ADV-2', country, seller_id, category, impressions, clicks,
               30, avg_cpc, bid, daily_budget, spent_today, planned_cum_share
        FROM ads_hourly WHERE country='DE';
    """)
    response = client.get("/data/ads/auction", params=WINDOW)
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["top_advertisers"] == ["ADV-1", "ADV-2"]
    assert row["bidders"] == 2
    assert row["filled_slots"] == 300
    assert row["slots"] == 200
    connection.execute("DELETE FROM ads_hourly")
    assert (
        client.get("/data/ads/auction", params=WINDOW).json()["rows"][0][
            "top_advertisers"
        ]
        is None
    )
