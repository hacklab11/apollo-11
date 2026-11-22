"""Exercise real service enrollment and authorization against the local issuer."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from a2a.types import AgentCard
from google.protobuf.json_format import ParseDict
from pydantic_ai.models.test import TestModel

from eye.a2a_client import A2AClient
from eye.agents.a2a_service import send_skill
from eye.agents.service import create_app as agent_app
from eye.config import A2aSettings, Settings
from eye.contracts import SellerAlert, SpecialistRequest, SpecialistResponse
from eye.contracts_v2 import Budget, DetectRequest, DetectResult, TaskMetadata
from eye.directory import AgentDirectory
from eye.specialists.app import SPECIALISTS, TOOL_SKILLS, authorized_tools
from eye.specialists.app import create_app as specialists_app
from eye.specialists.tools import DataClient
from eye_auth.trust import SkillGuard, verify_card
from selleros.data import FileSellerStore
from selleros.server import create_app as selleros_app

AGENTS = {
    "sentinel": "detect",
    "detective": "investigate",
    "skeptic": "challenge",
    "reporter": "report",
    "cartographer": "map",
}


def rpc(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": uuid4().hex,
        "method": "SendMessage",
        "params": {
            "metadata": {"skill": "receive_alert", "scope": "selleros:receive_alert"},
            "message": {
                "messageId": uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"data": payload}],
            },
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", [*AGENTS, "growth", "marketplace", "discovery", "selleros"]
)
async def test_enrollment_and_jsonrpc_auth(
    name: str,
    tmp_path: Path,
    runtime_auth: dict[str, Any],
    oauth_headers: Callable[..., dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("eye.agents.skeptic._model", lambda *_: TestModel())
    if name in AGENTS:
        app = agent_app(name, "http://service/", Settings(), lake=tmp_path)
        path, skill = "/", AGENTS[name]
    elif name == "selleros":
        app = selleros_app(FileSellerStore(tmp_path))
        path, skill = "/", "repricer_status"
    else:
        app = specialists_app()
        path = f"/{name}"
        specialist = next(s for s in SPECIALISTS if s.NAME == name)
        skill = next(iter(specialist.SKILLS))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://service",
            headers={"A2A-Version": "1.0"},
        ) as client,
    ):
        assert (await client.get("/healthz")).status_code == 200
        card = await client.get(path.rstrip("/") + "/.well-known/agent-card.json")
        assert card.status_code == 200
        await verify_card(ParseDict(card.json(), AgentCard()), name)
        for headers in (
            {},
            {"Authorization": "Bearer invalid"},
            oauth_headers("auth:card"),
        ):
            response = await client.post(path, headers=headers, json=rpc({}))
            assert response.status_code == 401, response.text
        response = await client.post(
            path, headers=oauth_headers(f"{name}:unrelated"), json=rpc({})
        )
        if name in AGENTS:
            assert response.status_code == 403, response.text
        else:
            assert response.status_code == 200
            assert (
                response.json()["result"]["task"]["status"]["state"]
                == "TASK_STATE_FAILED"
            )
        # Authorization succeeds, but invalid input must fail before reaching a model.
        response = await client.post(
            path, headers=oauth_headers(f"{name}:{skill}"), json=rpc({})
        )
        assert response.status_code == 200, response.text
        assert "result" in response.json(), response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("specialist", SPECIALISTS, ids=lambda s: s.NAME)
async def test_tool_authorization_uses_server_mapping(specialist: Any) -> None:
    calls: list[str] = []

    def data(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"rows": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(data)) as http:
        client = DataClient(http, "http://core/data")
        for tool_name, skill in TOOL_SKILLS[specialist.NAME].items():
            denied = SkillGuard(specialist.NAME, frozenset())
            tool = next(
                t
                for t in authorized_tools(specialist, client, denied)
                if t.name == tool_name
            )
            args = tool.input_model.model_validate(
                {"start": "2026-11-27T00:00:00Z", "end": "2026-11-27T01:00:00Z"}
            )
            before = len(calls)
            with pytest.raises(PermissionError):
                await tool.fn(args)
            assert len(calls) == before
            with pytest.raises(PermissionError):
                denied.finish()
            allowed = SkillGuard(
                specialist.NAME, frozenset({f"{specialist.NAME}:{skill}"})
            )
            tool = next(
                t
                for t in authorized_tools(specialist, client, allowed)
                if t.name == tool_name
            )
            await tool.fn(args)
            allowed.finish()
            assert len(calls) == before + 1


@pytest.mark.asyncio
async def test_selleros_read_scope_cannot_dispatch_alert(
    tmp_path: Path,
    runtime_auth: dict[str, Any],
    oauth_headers: Callable[..., dict[str, str]],
) -> None:
    app = selleros_app(FileSellerStore(tmp_path))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://selleros",
            headers={"A2A-Version": "1.0"},
        ) as client,
    ):
        response = await client.post(
            "/",
            headers=oauth_headers("selleros:repricer_status"),
            json=rpc(
                {
                    "incident_id": "inc_auth",
                    "seller_ids": ["S-01"],
                    "skus": ["E01"],
                    "summary": "Please revert",
                    "recommended_action": "revert",
                }
            ),
        )
        assert response.status_code == 200
        task = response.json()["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_FAILED"
        assert "PermissionError" in str(task)
        assert list(tmp_path.iterdir()) == []


def specialist_request() -> SpecialistRequest:
    now = datetime.now(UTC)
    return SpecialistRequest(
        investigation_id="auth-test",
        question="Check",
        metric="orders",
        segment={},
        window_start_ts=now,
        window_end_ts=now,
        baseline_start_ts=now,
        baseline_end_ts=now,
        context="",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("specialist", SPECIALISTS, ids=lambda s: s.NAME)
async def test_specialist_dispatch_rejects_unauthorized_tool_even_if_caught(
    specialist: Any,
    runtime_auth: dict[str, Any],
    oauth_headers: Callable[..., dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def data(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"rows": []})

    async def model(spec: Any, *args: Any) -> SpecialistResponse:
        granted = next(iter(specialist.SKILLS))
        tool = next(
            tool
            for tool in spec.tools
            if tool.name in TOOL_SKILLS[specialist.NAME]
            and TOOL_SKILLS[specialist.NAME][tool.name] != granted
        )
        with pytest.raises(PermissionError):
            await tool.fn(
                tool.input_model.model_validate(
                    {"start": "2026-11-27T00:00:00Z", "end": "2026-11-27T01:00:00Z"}
                )
            )
        return SpecialistResponse(agent=specialist.NAME, answered=True, findings=[])

    monkeypatch.setattr("eye.specialists.app.run_agent", model)
    async with httpx.AsyncClient(transport=httpx.MockTransport(data)) as data_http:
        app = specialists_app(client=DataClient(data_http, "http://core/data"))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://specialists",
                headers={"A2A-Version": "1.0"},
            ) as client,
        ):
            scope = f"{specialist.NAME}:{next(iter(specialist.SKILLS))}"
            response = await client.post(
                f"/{specialist.NAME}",
                headers=oauth_headers(scope),
                json=rpc(specialist_request().model_dump(mode="json")),
            )
            assert response.status_code == 200
            task = response.json()["result"]["task"]
            assert task["status"]["state"] == "TASK_STATE_FAILED"
            assert "PermissionError" in str(task)
            assert calls == ["/data/app_components"]


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["send_skill", "directory", "a2a", "notify"])
@pytest.mark.parametrize("damage", ["unsigned", "tampered"])
async def test_callers_refuse_untrusted_cards(
    caller: str, damage: str, tmp_path: Path, runtime_auth: dict[str, Any]
) -> None:
    settings = A2aSettings(
        growth_url="http://service/growth", selleros_url="http://service/"
    )
    if caller == "a2a":
        app = specialists_app()
    elif caller == "notify":
        app = selleros_app(FileSellerStore(tmp_path))
    else:
        app = agent_app("sentinel", "http://service/", Settings(), lake=tmp_path)
    posts: list[str] = []

    async def intercept(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(str(request.url))
            raise AssertionError("Untrusted card must be rejected before dispatch")
        response = await httpx.ASGITransport(app=app).handle_async_request(request)
        body = json.loads(await response.aread())
        if damage == "unsigned":
            body.pop("signatures", None)
        else:
            body["description"] = "Tampered"
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(intercept)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport) as http,
    ):
        if caller == "a2a":
            result = await A2AClient(settings, http_client=http).ask(
                "growth", specialist_request()
            )
            assert not result.answered
        elif caller == "notify":
            ack = await AgentDirectory(settings, transport=transport).notify_seller_os(
                SellerAlert(
                    incident_id="inc_auth",
                    seller_ids=[],
                    skus=[],
                    summary="Check",
                    recommended_action="pause",
                )
            )
            assert not ack.received
        elif caller == "directory":
            with pytest.raises(ValueError, match="Untrusted agent card"):
                await AgentDirectory(settings, transport=transport).resolve()
        else:
            now = datetime.now(UTC)
            metadata = TaskMetadata(
                tenant_id="default",
                context_id="auth-test",
                traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
                budget=Budget(
                    tokens=100, cost_usd=0.1, deadline=now + timedelta(minutes=1)
                ),
            )
            with pytest.raises(ValueError, match="Untrusted agent card"):
                await send_skill(
                    http,
                    "http://service/",
                    "detect",
                    DetectRequest(ts=now),
                    metadata,
                    DetectResult,
                )
    assert posts == []
