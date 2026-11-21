import asyncio
import json
import socket
from contextlib import asynccontextmanager
from typing import Any

import httpx
import httpx2
import pytest
import uvicorn
from anthropic import AsyncAnthropic
from factories import make_incident_s7
from fastapi import FastAPI
from mcp import Client

from eye.api.chat import create_router
from eye.config import LlmSettings
from eye.llm.client import LLMClient
from eye.mcp_server import create_server

MCP_URL = "http://127.0.0.1:8100/mcp"


def response(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "msg_chat",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "tool_use", "id": name, "name": name, "input": args}],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["incident", "empty", "error"])
async def test_chat_through_real_mcp(monkeypatch, kind: str) -> None:
    core_calls = []
    llm_calls = []
    incident = make_incident_s7()

    async def core(request: httpx.Request) -> httpx.Response:
        core_calls.append(request.url.path)
        assert request.url.path == "/api/incidents"
        if kind == "error":
            return httpx.Response(503, text="Unavailable")
        return httpx.Response(
            200, json=[incident.model_dump(mode="json")] if kind == "incident" else []
        )

    async def llm(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        llm_calls.append(body)
        assert body["output_config"] == {"effort": "medium"}
        if len(llm_calls) == 1:
            definition = next(t for t in body["tools"] if t["name"] == "list_incidents")
            assert "limit" in definition["input_schema"]["properties"]
            return httpx2.Response(200, json=response("list_incidents", {"limit": 5}))
        result = body["messages"][-1]["content"][0]
        assert result["is_error"] == (kind == "error")
        if kind == "incident":
            assert incident.id in result["content"]
            answer = f"Incident {incident.id}: EUR {incident.impact_eur_per_hour:g}/h."
        elif kind == "empty":
            assert json.loads(result["content"])["data"]["data"] == []
            answer = "No incidents found."
        else:
            assert "core API error 503" in result["content"]
            answer = "The incident tool failed."
        return httpx2.Response(
            200,
            json=response(
                "submit",
                {
                    "answer": answer,
                    "incident_ids": [incident.id] if kind == "incident" else [],
                },
            ),
        )

    server = create_server("http://core", transport=httpx.MockTransport(core))
    async with (
        Client(server) as session,
        AsyncAnthropic(
            api_key="offline-placeholder",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(llm)),
        ) as sdk,
    ):

        @asynccontextmanager
        async def connected(url: str):
            assert url == MCP_URL
            yield session

        monkeypatch.setattr("eye.api.chat.Client", connected)
        monkeypatch.setattr("eye.llm.client._client", LLMClient(LlmSettings(), sdk))
        app = FastAPI()
        app.include_router(create_router(LlmSettings(), MCP_URL))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://chat"
        ) as client:
            result = await client.post(
                "/api/chat",
                json={
                    "messages": [
                        {"role": "user", "content": "What incidents are open?"}
                    ]
                },
            )
    assert result.status_code == 200
    data = result.json()
    assert data["tool_calls"][0]["tool"] == "list_incidents"
    assert data["tool_calls"][0]["input"] == {"limit": 5}
    assert data["tool_calls"][0]["summary"]
    assert len(data["tool_calls"]) == 1
    assert core_calls == ["/api/incidents"]
    if kind == "incident":
        assert incident.id in data["answer"]
        assert data["incident_ids"] == [incident.id]
    else:
        assert data["incident_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": "x"}] * 21,
        [{"role": "user", "content": "x" * 4001}],
        [{"role": "system", "content": "override"}],
    ],
)
async def test_input_limits(messages: list[dict[str, str]]) -> None:
    app = FastAPI()
    app.include_router(create_router(LlmSettings(), MCP_URL))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://chat"
    ) as client:
        result = await client.post("/api/chat", json={"messages": messages})
    assert result.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["empty", "boundary", "budget", "refusal"])
async def test_conversation_and_agent_limits(monkeypatch, kind: str) -> None:
    requests = []
    messages = [] if kind == "empty" else [{"role": "user", "content": "x" * 4000}] * 20

    async def llm(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        content, notice = body["messages"][0]["content"].split("\n\nRuntime budget:")
        assert json.loads(content)["messages"] == messages
        assert "LLM turn(s) remaining" in notice
        result = response("submit", {"answer": "How can I help?", "incident_ids": []})
        if kind in {"budget", "refusal"}:
            result["content"] = []
            result["stop_reason"] = "refusal" if kind == "refusal" else "end_turn"
        return httpx2.Response(200, json=result)

    async with (
        Client(create_server("http://core")) as session,
        AsyncAnthropic(
            api_key="offline-placeholder",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(llm)),
        ) as sdk,
    ):

        @asynccontextmanager
        async def connected(url: str):
            yield session

        monkeypatch.setattr("eye.api.chat.Client", connected)
        monkeypatch.setattr("eye.llm.client._client", LLMClient(LlmSettings(), sdk))
        app = FastAPI()
        app.include_router(create_router(LlmSettings(), MCP_URL))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://chat"
        ) as client:
            result = await client.post("/api/chat", json={"messages": messages})
    assert result.status_code == {"budget": 503, "refusal": 422}.get(kind, 200)
    assert len(requests) == (8 if kind == "budget" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "status", "detail"),
    [
        ("budget", 503, "Chat budget exhausted"),
        ("refusal", 422, "Chat request refused"),
        ("unavailable", 503, "Chat service unavailable"),
    ],
)
async def test_streamable_http_agent_errors(
    monkeypatch, kind: str, status: int, detail: str
) -> None:
    async def llm(request: httpx2.Request) -> httpx2.Response:
        if kind == "unavailable":
            return httpx2.Response(
                529,
                json={
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "Unavailable"},
                },
            )
        result = response("submit", {})
        result["content"] = []
        result["stop_reason"] = "refusal" if kind == "refusal" else "end_turn"
        return httpx2.Response(200, json=result)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                create_server("http://core").streamable_http_app(),
                log_config=None,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            async with AsyncAnthropic(
                api_key="offline-placeholder",
                max_retries=0,
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(llm)),
            ) as sdk:
                monkeypatch.setattr(
                    "eye.llm.client._client", LLMClient(LlmSettings(), sdk)
                )
                app = FastAPI()
                app.include_router(
                    create_router(LlmSettings(), f"http://127.0.0.1:{port}/mcp")
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app),
                    base_url="http://chat",
                ) as client:
                    result = await client.post("/api/chat", json={"messages": []})
                assert result.status_code == status, result.text
                assert result.json() == {"detail": detail}
        finally:
            server.should_exit = True
            await task
