"""Small deterministic S1/S7 worlds behind the real data API router."""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import duckdb
import httpx
import pytest
import pytest_asyncio
from a2a.client import ClientConfig, ClientFactory
from a2a.client.card_resolver import A2ACardResolver
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest, Task
from fastapi import FastAPI
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value
from starlette.applications import Starlette

from eye.warehouse.domain import create_router
from eye.config import A2aSettings, Settings
from eye.contracts import SpecialistRequest
from eye.sim.store import Store
from eye.specialists.app import create_app
from eye.specialists.tools import DataClient

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"
DAY = datetime(2026, 11, 27, tzinfo=UTC)
BASELINE_DAY = DAY - timedelta(days=7)
S7_TS = DAY + timedelta(hours=3)
SCHEDULE = [0.02, 0.04, 0.07, 0.10, 0.14, 0.18, 0.22, 0.26, 0.30, 0.34, 0.38, 0.42]
# Cumulative spend at each hourly row; the 10:00 row exhausts the daily budget.
ADV3_SPENT = [60, 140, 230, 330, 440, 560, 680, 790, 880, 950, 1000, 1000]


def _ts(value: datetime) -> str:
    return value.isoformat()


def _ads(con: duckdb.DuckDBPyConnection) -> None:
    baseline = [round(1000 * share * 1.02, 2) for share in SCHEDULE]
    control = [round(500 * share * 0.98, 2) for share in SCHEDULE]
    advertisers = [
        (DAY, "ADV-3", "S-03", 1000.0, 1.25, ADV3_SPENT),
        (BASELINE_DAY, "ADV-3", "S-03", 1000.0, 1.25, baseline),
        (DAY, "ADV-1", "S-01", 500.0, 1.0, control),
        (BASELINE_DAY, "ADV-1", "S-01", 500.0, 1.0, control),
    ]
    rows = []
    for day, advertiser, seller, budget, cpc, cumulative in advertisers:
        previous = 0.0
        for hour, (share, total) in enumerate(zip(SCHEDULE, cumulative, strict=True)):
            spend = round(total - previous, 2)
            previous = total
            clicks = round(spend / cpc)
            rows.append(
                (
                    _ts(day + timedelta(hours=hour)),
                    advertiser,
                    "DE",
                    seller,
                    "electronics",
                    clicks * 20,
                    clicks,
                    spend,
                    cpc if clicks else 0.0,
                    cpc + 0.15,
                    budget,
                    total,
                    share,
                )
            )
    con.executemany(f"INSERT INTO ads_hourly VALUES ({','.join('?' * 13)})", rows)
    con.execute(
        "INSERT INTO change_log VALUES ('chg_pacing','2026-11-26T23:00:00Z',"
        "'pacing-svc','release',true,'{\"country\":\"DE\"}',"
        "'pacing-svc v4.1 rollout','{}')"
    )


def _checkout(con: duckdb.DuckDBPyConnection) -> None:
    checkout, payments, errors = [], [], []
    for hour in range(7):
        ts = _ts(DAY + timedelta(hours=hour))
        broken = DAY + timedelta(hours=hour) >= S7_TS
        for platform, carts in (("web", 4000), ("android", 3000), ("ios", 2000)):
            rate = 0.09 if broken and platform != "ios" else 0.6
            starts = round(carts * rate)
            attempts = round(starts * 0.9)
            checkout.append((ts, "DE", platform, carts, starts, attempts))
            payments.append(
                (ts, "DE", platform, "PSP-2", attempts, round(attempts * 0.96), 820.0)
            )
            if platform != "ios":
                count = 15500 if broken else 3
                version = "5.8" if broken else "5.7"
                errors.append((ts, platform, version, "checkout", "TypeError", count))
    con.executemany("INSERT INTO checkout_hourly VALUES (?,?,?,?,?,?)", checkout)
    con.executemany("INSERT INTO payments_hourly VALUES (?,?,?,?,?,?,?)", payments)
    con.executemany("INSERT INTO client_errors_hourly VALUES (?,?,?,?,?,?)", errors)
    con.execute("""
        INSERT INTO app_components VALUES
          ('web','checkout','checkout-web','v5.8'),
          ('android','checkout','checkout-web','v5.8'),
          ('ios','checkout','native-ios','v3.2');
        INSERT INTO change_log VALUES ('chg_checkout','2026-11-27T03:00:00Z',
          'checkout-web','release',true,'{"platforms":["web","android"]}',
          'checkout-web v5.8 release','{}');
        INSERT INTO external_status VALUES
          ('ext_psp2','2026-11-27T02:00:00Z','PSP-2','ok','All systems operational');
    """)


@pytest.fixture(scope="session")
def world() -> Iterator[duckdb.DuckDBPyConnection]:
    store = Store(":memory:")
    with store.cursor() as con:
        _ads(con)
        _checkout(con)
        yield con
    store.close()


def settings() -> Settings:
    return Settings(
        a2a=A2aSettings(
            growth_url="http://specialists/growth",
            marketplace_url="http://specialists/marketplace",
            discovery_url="http://specialists/discovery",
            selleros_url="http://selleros",
        )
    )


def data_client(world: duckdb.DuckDBPyConnection) -> DataClient:
    core = FastAPI()
    core.include_router(create_router(world))
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=core))
    return DataClient(http, "http://core/data")


@pytest_asyncio.fixture
async def specialists(
    world: duckdb.DuckDBPyConnection,
) -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    client = data_client(world)
    app = create_app(settings(), client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        yield app, http
    await client.http.aclose()


def s1_request() -> SpecialistRequest:
    return SpecialistRequest(
        investigation_id="inv_s1",
        question="Is ADV-3's ad spend in DE electronics on schedule today?",
        metric="ad_spend",
        segment={"country": "DE", "category": "electronics"},
        window_start_ts=DAY,
        window_end_ts=DAY + timedelta(hours=12),
        baseline_start_ts=BASELINE_DAY,
        baseline_end_ts=BASELINE_DAY + timedelta(hours=12),
        context="DE electronics ad_spend is 2.6x expected since 06:00; ADV-3 is "
        "the largest advertiser.",
    )


def s7_request() -> SpecialistRequest:
    return SpecialistRequest(
        investigation_id="inv_s7",
        question="Why did DE checkout starts drop on web and android since 03:00?",
        metric="cart_to_checkout_rate",
        segment={"country": "DE"},
        window_start_ts=DAY,
        window_end_ts=DAY + timedelta(hours=7),
        baseline_start_ts=BASELINE_DAY,
        baseline_end_ts=BASELINE_DAY + timedelta(hours=7),
        context="DE orders fell 40% from 03:00 during Black Friday; carts are up.",
    )


async def send(http: httpx.AsyncClient, agent: str, request: SpecialistRequest) -> Task:
    card = await A2ACardResolver(http, f"http://specialists/{agent}").get_agent_card()
    message = SendMessageRequest(
        message=Message(
            message_id=uuid4().hex,
            role=Role.ROLE_USER,
            parts=[
                Part(data=ParseDict(request.model_dump(mode="json"), Value())),
                Part(text=request.question),
            ],
        )
    )
    factory = ClientFactory(ClientConfig(streaming=False, httpx_client=http))
    async with factory.create(card) as client:
        async for event in client.send_message(message):
            return event.task
    raise AssertionError("no task returned")
