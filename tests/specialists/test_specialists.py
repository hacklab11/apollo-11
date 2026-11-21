import asyncio
import time
from pathlib import Path

import httpx
import httpx2
import pytest
from a2a.types.a2a_pb2 import TaskState
from anthropic import AsyncAnthropic
from conftest import FIXTURES, data_client, s1_request, s7_request, send, settings
from google.protobuf.json_format import MessageToDict

from eye.a2a_client import A2AClient
from eye.contracts import SpecialistResponse
from eye.llm.client import LLMClient
from eye.specialists.app import create_app
from eye.specialists.tools import DataClient

SKILLS = {
    "growth": ["ads_pacing", "auction_health", "traffic_sources", "discounts"],
    "marketplace": ["buybox", "pricing", "availability", "purchasing", "new_sellers"],
    "discovery": [
        "search_quality",
        "feed_exposure",
        "checkout_funnel",
        "client_errors",
        "components",
    ],
}
CASES = {
    "growth": (s1_request, FIXTURES / "specialist_s1_growth.jsonl"),
    "discovery": (s7_request, FIXTURES / "specialist_s7_discovery.jsonl"),
}


def offline_sdk() -> AsyncAnthropic:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"replay miss reached the network: {request.url}")

    return AsyncAnthropic(
        api_key="offline",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(refuse)),
    )


def use_llm(monkeypatch: pytest.MonkeyPatch, mode: str, fixture: Path, sdk) -> None:
    llm = settings().llm.model_copy(update={"mode": mode, "cache_path": fixture})
    monkeypatch.setattr("eye.llm.client._client", LLMClient(llm, sdk))


async def ask(
    http: httpx.AsyncClient, agent: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[SpecialistResponse, list[dict[str, str]]]:
    request, fixture = CASES[agent]
    use_llm(monkeypatch, "replay", fixture, offline_sdk())
    task = await send(http, agent, request())
    assert task.status.state == TaskState.TASK_STATE_COMPLETED, MessageToDict(
        task.status
    )
    (artifact,) = task.artifacts
    (part,) = artifact.parts
    steps = MessageToDict(artifact.metadata)["steps"]
    return SpecialistResponse.model_validate(MessageToDict(part.data)), steps


@pytest.mark.asyncio
async def test_each_card_is_served_with_its_skills(specialists) -> None:
    _, http = specialists
    for agent, skills in SKILLS.items():
        response = await http.get(
            f"http://specialists/{agent}/.well-known/agent-card.json"
        )
        assert response.status_code == 200
        card = response.json()
        assert [skill["id"] for skill in card["skills"]] == skills
        assert card["supportedInterfaces"][0]["url"] == f"http://specialists/{agent}"


@pytest.mark.asyncio
async def test_core_client_sees_all_three_specialists(specialists) -> None:
    _, http = specialists
    client = A2AClient(settings().a2a, http_client=http)
    cards = await client.fetch_cards()
    for agent, skills in SKILLS.items():
        assert cards[agent].healthy and cards[agent].skills == skills


@pytest.mark.asyncio
async def test_growth_replay_answers_s1(specialists, monkeypatch) -> None:
    _, http = specialists
    response, steps = await ask(http, "growth", monkeypatch)
    assert response.answered and response.agent == "growth"
    finding = next(f for f in response.findings if "pacing_bug" in f.supports)
    for text in ("ADV-3", "100%", "10:00", "38%"):
        assert text in finding.claim
    assert all("traffic_change" not in f.supports for f in response.findings)
    assert "ads/pacing" in response.data_used
    assert "ads_pacing" in {step["tool"] for step in steps}
    assert all(set(step) == {"tool", "summary"} for step in steps)


@pytest.mark.asyncio
async def test_discovery_replay_answers_s7(specialists, monkeypatch) -> None:
    _, http = specialists
    response, steps = await ask(http, "discovery", monkeypatch)
    assert response.answered and response.agent == "discovery"
    claims = " ".join(f.claim for f in response.findings)
    assert "checkout-web v5.8" in claims and "TypeError" in claims
    # 4 broken hours x 15,500 checkout TypeErrors per platform in the S7 world.
    assert any(
        "62,000" in f.claim and 62000 in f.numbers.values() for f in response.findings
    )
    assert "errors" in response.data_used
    assert {s["tool"] for s in steps} >= {"errors", "checkout_funnel"}


@pytest.mark.asyncio
async def test_data_api_down_fails_task_promptly(monkeypatch) -> None:
    use_llm(monkeypatch, "replay", CASES["growth"][1], offline_sdk())

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    dead = DataClient(
        httpx.AsyncClient(transport=httpx.MockTransport(down)), "http://core/data"
    )
    app = create_app(settings(), dead)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        started = time.monotonic()
        task = await asyncio.wait_for(send(http, "growth", s1_request()), timeout=5)
        assert time.monotonic() - started < 5
        assert task.status.state == TaskState.TASK_STATE_FAILED
        text = task.status.message.parts[0].text
        assert "core data API unavailable at http://core/data: ConnectError" in text
        assert task.artifacts == []
        core = await A2AClient(settings().a2a, http_client=http).ask(
            "growth", s1_request()
        )
        assert not core.answered and core.findings == []


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize("agent", sorted(CASES))
async def test_record_fixture(agent: str, world, monkeypatch) -> None:
    """Re-record with: uv run pytest tests/specialists -m live -q"""
    request, fixture = CASES[agent]
    fixture.unlink(missing_ok=True)
    async with AsyncAnthropic(max_retries=2) as sdk:
        use_llm(monkeypatch, "record", fixture, sdk)
        client = data_client(world)
        app = create_app(settings(), client)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            task = await send(http, agent, request())
        await client.http.aclose()
    assert task.status.state == TaskState.TASK_STATE_COMPLETED, MessageToDict(
        task.status
    )
    assert Path(fixture).read_text().strip()


@pytest.mark.asyncio
async def test_truncated_rows_are_flagged_in_summary() -> None:
    rows = [{"ts": f"2026-11-27T{i % 24:02d}:00:00Z", "n": i} for i in range(250)]
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"rows": rows}))
    async with httpx.AsyncClient(transport=transport) as http:
        result = await DataClient(http, "http://core/data").get("offers", {})
    assert result.total_rows == 250 and len(result.rows) == 200
    assert "only the first 200 shown" in result.summary
    assert "narrow the window" in result.summary
