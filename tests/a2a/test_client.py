import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from stub_server import EchoExecutor, stub_server

from eye.a2a_client import A2AClient
from eye.config import A2aSettings
from eye.contracts import SellerAlert, SpecialistRequest
from eye.directory import AgentDirectory


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
            f"{agent}_url": f"http://{agent}"
            for agent in ("growth", "marketplace", "discovery", "selleros")
        }
    )


@pytest.mark.asyncio
async def test_round_trip_and_health() -> None:
    _, executor = stub_server()
    apps = {
        name: stub_server(executor, name=name)[0]
        for name in ("growth", "marketplace", "discovery", "selleros")
    }

    async def handle(req: httpx.Request) -> httpx.Response:
        return await httpx.ASGITransport(app=apps[req.url.host]).handle_async_request(
            req
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
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


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_directory_delivers_seller_alerts(failed: bool) -> None:
    app, executor = stub_server(EchoExecutor(failed=failed), name="selleros")
    directory = AgentDirectory(settings(), transport=httpx.ASGITransport(app=app))
    alert = SellerAlert(
        incident_id="inc_1",
        seller_ids=["S-01"],
        skus=[],
        summary="Price loop",
        recommended_action="Review",
    )
    ack = await directory.notify_seller_os(alert)
    assert ack.received == (not failed)
    assert executor.requests == [alert]


@pytest.mark.asyncio
async def test_directory_skips_invalid_card_and_keeps_healthy_agent(caplog) -> None:
    apps = {name: stub_server(name=name)[0] for name in ("sentinel", "detective")}

    async def handle(req: httpx.Request) -> httpx.Response:
        if req.url.host not in apps:
            return httpx.Response(404)
        response = await httpx.ASGITransport(
            app=apps[req.url.host]
        ).handle_async_request(req)
        if req.url.host == "detective":
            await response.aread()
            body = response.json()
            body.pop("signatures", None)
            return httpx.Response(200, json=body)
        return response

    directory = AgentDirectory(
        A2aSettings(
            **{
                f"{name}_url": f"http://{name}"
                for name in (
                    "sentinel",
                    "detective",
                    "skeptic",
                    "reporter",
                    "cartographer",
                )
            }
        ),
        transport=httpx.MockTransport(handle),
    )
    await directory.resolve()
    assert set(directory.cards) == {"echo"}
    assert directory.cards["echo"].name == "sentinel"
    assert "Skipping untrusted A2A card for detective" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "rejected"])
async def test_seller_delivery_returns_failure_on_oauth_error(
    failure, monkeypatch
) -> None:
    app, executor = stub_server(name="selleros")
    if failure == "missing":
        monkeypatch.delenv("EYE_AUTH_EYE_CORE_SECRET", raising=False)
        monkeypatch.delenv("EYE_AUTH_CLIENT_SECRET", raising=False)
    else:
        monkeypatch.setenv("EYE_AUTH_EYE_CORE_SECRET", "invalid-test-only")
    directory = AgentDirectory(settings(), transport=httpx.ASGITransport(app=app))
    ack = await directory.notify_seller_os(
        SellerAlert(
            incident_id="inc_1",
            seller_ids=[],
            skus=[],
            summary="Check",
            recommended_action="Review",
        )
    )
    assert not ack.received and not ack.actions_taken
    assert not executor.requests
