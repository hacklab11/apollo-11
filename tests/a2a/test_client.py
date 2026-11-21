import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from stub_server import EchoExecutor, stub_server

from eye.a2a_client import A2AClient
from eye.config import A2aSettings
from eye.contracts import SellerAlert, SpecialistRequest


def request() -> SpecialistRequest:
    ts = datetime(2026, 11, 27, tzinfo=UTC)
    return SpecialistRequest(
        investigation_id="inv_test",
        question="How is pacing?",
        metric="ad_spend",
        segment={"country": "DE"},
        window_start_ts=ts,
        window_end_ts=ts + timedelta(hours=1),
        baseline_start_ts=ts - timedelta(days=7),
        baseline_end_ts=ts - timedelta(days=7) + timedelta(hours=1),
        context="Check the advertiser",
    )


def settings() -> A2aSettings:
    return A2aSettings(
        **{
            f"{agent}_url": "http://stub"
            for agent in ("growth", "marketplace", "discovery", "selleros")
        }
    )


@pytest.mark.asyncio
async def test_round_trip_and_health() -> None:
    app, executor = stub_server()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        client = A2AClient(settings(), http_client=http)
        cards = await client.fetch_cards()
        assert set(cards) == {"growth", "marketplace", "discovery", "selleros"}
        assert all(card.healthy and card.skills == ["echo"] for card in cards.values())
        result = await client.ask("growth", request())
        assert result.answered and result.agent == "growth" and result.findings == []
        assert executor.requests == [request()]
        assert executor.texts == [request().question]
        alert = SellerAlert(
            incident_id="inc_1",
            seller_ids=["S-01"],
            skus=["E01"],
            summary="Price loop",
            recommended_action="Review",
        )
        ack = await client.notify_seller_os(alert)
        assert ack.received and ack.actions_taken == ["review scheduled"]
        assert executor.requests[-1] == alert
        await client.aclose()
        assert not http.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["timeout", "connection", "json", "invalid", "failed", "empty", "mismatch"],
)
async def test_safe_failures(failure: str) -> None:
    payloads = {
        "invalid": {"answered": "not a boolean"},
        "empty": {},
        "mismatch": {"agent": "discovery", "answered": True, "findings": []},
    }
    app, _ = stub_server(
        EchoExecutor(payloads.get(failure), failed=failure == "failed")
    )
    asgi = httpx.ASGITransport(app=app)

    async def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            if failure == "timeout":
                await asyncio.sleep(1)
            if failure == "connection":
                raise httpx.ConnectError("refused", request=req)
            if failure == "json":
                return httpx.Response(200, text="not json")
        return await asgi.handle_async_request(req)

    traces: list[tuple[str, str]] = []
    config = settings()
    config.timeout_s = 0.05
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = A2AClient(
            config, http_client=http, on_unavailable=lambda a, r: traces.append((a, r))
        )
        response = await client.ask("growth", request())
        assert not response.answered and response.findings == []
        assert traces[-1][0] == "growth" and traces[-1][1].startswith(
            "agent unavailable: "
        )
        ack = await client.notify_seller_os(
            SellerAlert(
                incident_id="inc_1",
                seller_ids=[],
                skus=[],
                summary="",
                recommended_action="",
            )
        )
        assert not ack.received


@pytest.mark.asyncio
async def test_unavailable_cards_and_unknown_agent() -> None:
    async def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = A2AClient(settings(), http_client=http)
        cards = await client.fetch_cards()
        assert len(cards) == 4 and all(not card.healthy for card in cards.values())
        assert not (await client.ask("unknown", request())).answered
        cards = await client.fetch_cards()
        assert set(cards) == {"growth", "marketplace", "discovery", "selleros"}
        assert "unknown" not in client._health


@pytest.mark.asyncio
async def test_owned_http_client_closed() -> None:
    client = A2AClient(settings())
    await client.aclose()
    assert client._http.is_closed
