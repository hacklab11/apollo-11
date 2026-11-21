import asyncio
import json
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
import uvicorn
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent
from mcp_fake_core import Calls, fake_core
from pydantic import TypeAdapter

from eye.api.app import MetricDefinition
from eye.contracts import AddMetricResult, GraphView, ScenarioState, Series
from eye.mcp_server import (
    SCENARIO_TOOLS,
    IncidentDetail,
    IncidentSummary,
    create_server,
)

TOOLS = {
    "system_status",
    "list_incidents",
    "get_incident",
    "get_graph",
    "query_metric",
    "explain_metric",
    "add_metric",
    "list_scenarios",
    "inject_scenario",
    "clear_scenario",
}


def core_calls(calls: Calls) -> Calls:
    """Drop the scenario-source probe that every tool listing makes."""
    return [call for call in calls if call != ("/api/scenarios", {})]


def text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def payload(result: CallToolResult) -> Any:
    assert not result.is_error, text(result)
    summary, body = text(result).split("\n", 1)
    assert 0 < len(summary) <= 300
    assert result.structured_content == {"summary": summary, "data": json.loads(body)}
    return json.loads(body)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest_asyncio.fixture
async def http_url(server: MCPServer) -> AsyncIterator[str]:
    port = free_port()
    http = uvicorn.Server(
        uvicorn.Config(
            server.streamable_http_app(), host="127.0.0.1", port=port, log_config=None
        )
    )
    task = asyncio.create_task(http.serve())
    while not http.started:
        await asyncio.sleep(0.01)
    yield f"http://127.0.0.1:{port}/mcp"
    http.should_exit = True
    await task


@pytest.mark.asyncio
async def test_streamable_http_lists_ten_described_tools(http_url: str) -> None:
    async with Client(http_url) as client:
        tools = (await client.list_tools()).tools
        status = payload(await client.call_tool("system_status", {}))
        assert client.server_info is not None
        assert client.server_info.name == "eye-of-the-system"
        assert client.instructions and "system_status" in client.instructions
    assert {tool.name for tool in tools} == TOOLS
    for tool in tools:
        assert tool.description and "eturn" in tool.description
        assert "Use it" in tool.description
    assert status["tick"] == 14


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenarios",
    [[], {"detail": "not a catalogue"}, None],
    ids=["no-scenario-source", "invalid-catalogue", "core-unavailable"],
)
async def test_scenario_tools_hidden_without_scenario_source(
    scenarios: object,
) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("core down", request=request)

    transport = (
        httpx.ASGITransport(app=fake_core([], scenario_list=scenarios))
        if scenarios is not None
        else httpx.MockTransport(unavailable)
    )
    async with Client(create_server("http://core", transport=transport)) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == TOOLS - SCENARIO_TOOLS


@pytest.mark.asyncio
async def test_list_incidents_returns_valid_json(
    server: MCPServer, calls: Calls
) -> None:
    async with Client(server) as client:
        result = await client.call_tool(
            "list_incidents", {"status": "open", "limit": 5}
        )
    incidents = TypeAdapter(list[IncidentSummary]).validate_python(payload(result))
    assert [incident.id for incident in incidents] == ["inc_s6", "inc_s7"]
    assert text(result).startswith("2 of 2 incidents with status open")
    assert core_calls(calls) == [("/api/incidents", {"status": "open"})]


@pytest.mark.asyncio
async def test_list_incidents_limit_keeps_newest(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("list_incidents", {"limit": 1})
    assert [incident["id"] for incident in payload(result)] == ["inc_s6"]


@pytest.mark.asyncio
async def test_invalid_scenario_is_rejected_without_api_call(
    server: MCPServer, calls: Calls
) -> None:
    async with Client(server) as client:
        result = await client.call_tool("inject_scenario", {"id": "S9"})
    assert result.is_error
    assert "validation error" in text(result)
    assert calls == []


@pytest.mark.asyncio
async def test_inject_and_clear_scenario(server: MCPServer, calls: Calls) -> None:
    async with Client(server) as client:
        injected = payload(await client.call_tool("inject_scenario", {"id": "S8"}))
        cleared = payload(await client.call_tool("clear_scenario", {"id": "S8"}))
    assert ScenarioState.model_validate(injected).active
    assert not ScenarioState.model_validate(cleared).active
    assert [path for path, _ in core_calls(calls)] == [
        "/api/scenarios/S8/inject",
        "/api/scenarios/S8/clear",
    ]


@pytest.mark.asyncio
async def test_get_incident_includes_trail_summaries(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("get_incident", {"id": "inc_s7"})
    detail = IncidentDetail.model_validate(payload(result))
    assert detail.incident.id == "inc_s7"
    assert len(detail.trail) == detail.trail_steps_total == 10


@pytest.mark.asyncio
async def test_core_error_becomes_tool_error(server: MCPServer) -> None:
    async with Client(server) as client:
        result = await client.call_tool("get_incident", {"id": "inc_nope"})
    assert result.is_error
    assert "core API error 404: incident inc_nope not found" in text(result)


@pytest.mark.asyncio
async def test_core_down_is_core_unavailable() -> None:
    async with Client(create_server(f"http://127.0.0.1:{free_port()}")) as client:
        result = await client.call_tool("list_incidents", {})
    assert result.is_error
    assert text(result).endswith("core unavailable")


@pytest.mark.asyncio
async def test_bad_arguments_are_rejected_without_api_call(
    server: MCPServer, calls: Calls
) -> None:
    async with Client(server) as client:
        results = [
            await client.call_tool("list_incidents", {"limit": 51}),
            await client.call_tool("get_graph", {"node": "orders", "depth": 4}),
            await client.call_tool(
                "add_metric",
                {"name": "x", "description": "x", "sql": "select 1", "expression": "a"},
            ),
        ]
    assert all(result.is_error for result in results)
    assert "exactly one of sql or expression" in text(results[2])
    assert calls == []


@pytest.mark.asyncio
async def test_query_metric_defaults_to_48_hours_before_sim_time(
    server: MCPServer, calls: Calls
) -> None:
    async with Client(server) as client:
        result = await client.call_tool(
            "query_metric", {"name": "orders", "segment": {"country": "DE"}}
        )
    assert Series.model_validate(payload(result)).metric == "orders"
    path, params = core_calls(calls)[-1]
    end = datetime(2026, 11, 27, 14, tzinfo=UTC)
    assert path == "/api/metrics/orders"
    assert datetime.fromisoformat(params["start"]) == end - timedelta(hours=48)
    assert datetime.fromisoformat(params["end"]) == end
    assert json.loads(params["segment"]) == {"country": "DE"}


@pytest.mark.asyncio
async def test_other_tools_return_contract_json(server: MCPServer) -> None:
    async with Client(server) as client:
        graph = payload(await client.call_tool("get_graph", {}))
        definition = payload(
            await client.call_tool("explain_metric", {"name": "orders"})
        )
        scenarios = payload(await client.call_tool("list_scenarios", {}))
        added = payload(
            await client.call_tool(
                "add_metric",
                {
                    "name": "aov",
                    "description": "Order value",
                    "expression": "gmv / orders",
                },
            )
        )
    assert len(GraphView.model_validate(graph).nodes) == 12
    assert MetricDefinition.model_validate(definition).name == "orders"
    assert len(TypeAdapter(list[ScenarioState]).validate_python(scenarios)) == 9
    assert AddMetricResult.model_validate(added).metric.name
