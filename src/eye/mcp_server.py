"""MCP server: a stateless adapter over the core public API."""

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated, Any

import httpx
import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from pydantic import AwareDatetime, BaseModel, Field, TypeAdapter, ValidationError

from eye.api.app import MetricDefinition
from eye.config import load_settings
from eye.contracts import (
    AddMetricRequest,
    AddMetricResult,
    Dimension,
    Edge,
    GraphView,
    Incident,
    IncidentStatus,
    ScenarioId,
    ScenarioState,
    Series,
    TrailStep,
    Verdict,
)
from eye.orchestrator import Status

NAME = "eye-of-the-system"
INSTRUCTIONS = (
    "Monitoring system for the marketplace. Start with system_status and "
    "list_incidents. Incidents contain evidence trails."
)
SCENARIO_TOOLS = frozenset({"list_scenarios", "inject_scenario", "clear_scenario"})
MAX_INCIDENTS = 50
MAX_TRAIL_STEPS = 60
MAX_GRAPH_DEPTH = 3
MAX_GRAPH_NODES = 80
DEFAULT_WINDOW = timedelta(hours=48)
MAX_SUMMARY = 300
# add_metric runs the Cartographer, which may take up to 60 s.
TIMEOUT = httpx.Timeout(30.0, read=90.0)

_INCIDENTS = TypeAdapter(list[Incident])
_TRAIL = TypeAdapter(list[TrailStep])
_SCENARIOS = TypeAdapter(list[ScenarioState])

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, open_world_hint=False)


class IncidentSummary(BaseModel):
    id: str
    title: str
    status: IncidentStatus
    verdict: Verdict | None
    impact_eur_per_hour: float
    impact_eur_total: float
    opened_ts: datetime
    updated_ts: datetime


class TrailStepSummary(BaseModel):
    investigation_id: str
    n: int
    actor: str
    action: str
    tool: str | None
    output_summary: str
    reason: str


class IncidentDetail(BaseModel):
    incident: Incident
    trail: list[TrailStepSummary]
    trail_steps_total: int


def _result(summary: str, data: BaseModel | list[BaseModel]) -> CallToolResult:
    if len(summary) > MAX_SUMMARY:
        summary = summary[: MAX_SUMMARY - 1] + "…"
    payload = (
        [item.model_dump(mode="json") for item in data]
        if isinstance(data, list)
        else data.model_dump(mode="json")
    )
    return CallToolResult(
        content=[
            TextContent(type="text", text=f"{summary}\n{json.dumps(payload, indent=2)}")
        ],
        structured_content={"summary": summary, "data": payload},
    )


def _error_message(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", response.text)
    except (ValueError, AttributeError):
        detail = response.text
    return detail if isinstance(detail, str) else json.dumps(detail)


class _MonitoringServer(MCPServer):
    """Lists the scenario tools only while core reports a scenario source."""

    def __init__(self, has_scenarios: Callable[[], Awaitable[bool]]) -> None:
        super().__init__(NAME, instructions=INSTRUCTIONS, log_level="WARNING")
        self._has_scenarios = has_scenarios

    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        if await self._has_scenarios():
            return tools
        return [tool for tool in tools if tool.name not in SCENARIO_TOOLS]


def create_server(
    core_url: str, transport: httpx.AsyncBaseTransport | None = None
) -> MCPServer:
    async def call(
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: Any = None,
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=core_url, transport=transport, timeout=TIMEOUT
            ) as client:
                response = await client.request(method, path, params=params, json=body)
        except httpx.TransportError:
            raise ToolError("core unavailable") from None
        if response.is_error:
            raise ToolError(
                f"core API error {response.status_code}: {_error_message(response)}"
            )
        return response.json()

    async def has_scenarios() -> bool:
        try:
            return bool(_SCENARIOS.validate_python(await call("GET", "/api/scenarios")))
        except (ToolError, ValidationError):
            return False

    server = _MonitoringServer(has_scenarios)

    async def status() -> Status:
        return Status.model_validate(await call("GET", "/api/status"))

    @server.tool(annotations=READ_ONLY)
    async def system_status() -> CallToolResult:
        """Return the data clock and tick, agent health, active scenarios and
        LLM mode. Use it first to see whether the system is running."""
        state = await status()
        healthy = sum(agent.ok for agent in state.agents.values())
        active = ", ".join(state.active_scenarios) or "none"
        return _result(
            f"Data as of {state.sim_ts.isoformat()} (tick {state.tick}, "
            f"{'paused' if state.paused else 'running'}); {healthy}/{len(state.agents)} "
            f"agents healthy; active scenarios: {active}; LLM mode {state.llm_mode}; "
            f"{state.investigations_running} investigations running.",
            state,
        )

    @server.tool(annotations=READ_ONLY)
    async def list_incidents(
        status: Annotated[
            IncidentStatus | None, Field(description="Only incidents with this status.")
        ] = None,
        limit: Annotated[
            int,
            Field(description=f"Maximum incidents to return, 1 to {MAX_INCIDENTS}."),
        ] = 20,
    ) -> CallToolResult:
        """Return incidents with id, title, status, verdict, impact in EUR and
        opened/updated times, newest first. Use it to find what is wrong now;
        then call get_incident with an id for the evidence."""
        if not 1 <= limit <= MAX_INCIDENTS:
            raise ToolError(f"limit must be between 1 and {MAX_INCIDENTS}")
        params = {"status": status} if status is not None else None
        incidents = _INCIDENTS.validate_python(
            await call("GET", "/api/incidents", params=params)
        )
        shown = [
            IncidentSummary.model_validate(item, from_attributes=True)
            for item in sorted(
                incidents, key=lambda item: item.opened_ts, reverse=True
            )[:limit]
        ]
        impact = sum(item.impact_eur_per_hour for item in shown)
        return _result(
            f"{len(shown)} of {len(incidents)} incidents"
            f"{f' with status {status}' if status else ''}; "
            f"combined impact {impact:,.0f} EUR/h.",
            shown,
        )

    @server.tool(annotations=READ_ONLY)
    async def get_incident(
        id: Annotated[str, Field(description="Incident id, e.g. from list_incidents.")],
    ) -> CallToolResult:
        """Return one incident in full (verdict, root cause, hypotheses,
        contributions, suggested actions) with its evidence trail as step summaries
        (at most 60 steps). Use it to explain why an incident happened."""
        incident = Incident.model_validate(await call("GET", f"/api/incidents/{id}"))
        steps = _TRAIL.validate_python(await call("GET", f"/api/incidents/{id}/trail"))
        detail = IncidentDetail(
            incident=incident,
            trail=[
                TrailStepSummary.model_validate(step, from_attributes=True)
                for step in steps[:MAX_TRAIL_STEPS]
            ],
            trail_steps_total=len(steps),
        )
        return _result(
            f"{incident.id}: {incident.title} ({incident.status}, verdict "
            f"{incident.verdict or 'none'}, {incident.impact_eur_per_hour:,.0f} EUR/h); "
            f"trail shows {len(detail.trail)} of {len(steps)} steps.",
            detail,
        )

    @server.tool(annotations=READ_ONLY)
    async def get_graph(
        node: Annotated[
            str | None, Field(description="Center the view on this metric name.")
        ] = None,
        depth: Annotated[
            int | None,
            Field(description=f"Hops around node, 0 to {MAX_GRAPH_DEPTH}."),
        ] = None,
    ) -> CallToolResult:
        """Return the metric graph: nodes with their state and edges with type,
        status and confidence (at most 80 nodes). Use it to see how metrics relate
        or which ones are anomalous."""
        if depth is not None and not 0 <= depth <= MAX_GRAPH_DEPTH:
            raise ToolError(f"depth must be between 0 and {MAX_GRAPH_DEPTH}")
        params = {
            key: str(value)
            for key, value in {"node": node, "depth": depth}.items()
            if value is not None
        }
        view = GraphView.model_validate(
            await call("GET", "/api/graph", params=params or None)
        )
        nodes = view.nodes[:MAX_GRAPH_NODES]
        kept = {item["id"] for item in nodes}
        edges: list[Edge] = [
            edge for edge in view.edges if edge.src in kept and edge.dst in kept
        ]
        flagged = sum(item.get("state", "normal") != "normal" for item in nodes)
        return _result(
            f"Graph with {len(nodes)} of {len(view.nodes)} nodes and {len(edges)} "
            f"edges; {flagged} nodes not in normal state.",
            GraphView(nodes=nodes, edges=edges),
        )

    @server.tool(annotations=READ_ONLY)
    async def query_metric(
        name: Annotated[str, Field(description="Metric name, e.g. orders.")],
        start: Annotated[
            AwareDatetime | None,
            Field(description="Window start (ISO 8601). Default: end minus 48 h."),
        ] = None,
        end: Annotated[
            AwareDatetime | None,
            Field(description="Window end (ISO 8601). Default: latest data hour."),
        ] = None,
        segment: Annotated[
            dict[Dimension, str] | None,
            Field(description='Filter by dimension values, e.g. {"country": "DE"}.'),
        ] = None,
    ) -> CallToolResult:
        """Return a metric's hourly series with the expected value and lower/upper
        band per point. Use it to check whether a metric is off its baseline."""
        if start is None:
            end = end or (await status()).sim_ts
            start = end - DEFAULT_WINDOW
        params = {"start": start.isoformat(), "segment": json.dumps(segment or {})}
        if end is not None:
            params["end"] = end.isoformat()
        series = Series.model_validate(
            await call("GET", f"/api/metrics/{name}", params=params)
        )
        outside = sum(
            point.value is not None
            and point.lower is not None
            and point.upper is not None
            and not point.lower <= point.value <= point.upper
            for point in series.points
        )
        last = series.points[-1] if series.points else None
        latest = (
            f"; last {last.value} vs expected {last.expected} at {last.ts.isoformat()}"
            if last
            else ""
        )
        return _result(
            f"{name}: {len(series.points)} points, {outside} outside the expected "
            f"band{latest}.",
            series,
        )

    @server.tool(annotations=READ_ONLY)
    async def explain_metric(
        name: Annotated[str, Field(description="Metric name, e.g. orders.")],
    ) -> CallToolResult:
        """Return a metric's definition: description, SQL or formula, dimensions,
        and its incoming and outgoing edges with type and status. Use it to learn
        what a metric means and what drives it."""
        definition = MetricDefinition.model_validate(
            await call("GET", f"/api/metrics/{name}/definition")
        )
        return _result(
            f"{name} ({definition.kind}, {definition.unit}): {definition.description} "
            f"{len(definition.in_edges)} incoming and {len(definition.out_edges)} "
            "outgoing edges.",
            definition,
        )

    @server.tool(annotations=WRITES)
    async def add_metric(
        name: Annotated[str, Field(description="New metric name in snake_case.")],
        description: Annotated[str, Field(description="What the metric measures.")],
        sql: Annotated[
            str | None,
            Field(description="SQL source; give exactly one of sql or expression."),
        ] = None,
        expression: Annotated[
            str | None,
            Field(description="Formula over existing metrics, e.g. orders / sessions."),
        ] = None,
    ) -> CallToolResult:
        """Register a new metric and let the Cartographer connect it to the graph
        (may take up to 60 s). Returns the metric and its accepted, ambiguous and
        rejected edges. Use it only when the user asks to add a metric."""
        try:
            request = AddMetricRequest(
                name=name, description=description, sql=sql, expression=expression
            )
        except ValidationError as exc:
            raise ToolError(exc.errors()[0]["msg"]) from None
        result = AddMetricResult.model_validate(
            await call("POST", "/api/metrics", body=request.model_dump(mode="json"))
        )
        return _result(
            f"Added {result.metric.name}: {len(result.accepted)} accepted, "
            f"{len(result.ambiguous)} ambiguous, {len(result.rejected)} rejected edges.",
            result,
        )

    @server.tool(annotations=READ_ONLY)
    async def list_scenarios() -> CallToolResult:
        """Return the demo scenarios with id, title, whether each is active and when
        it was injected or cleared. Use it before inject_scenario or clear_scenario."""
        states = _SCENARIOS.validate_python(await call("GET", "/api/scenarios"))
        active = [state.id for state in states if state.active]
        return _result(
            f"{len(states)} scenarios, {len(active)} active"
            f"{': ' + ', '.join(active) if active else ''}.",
            states,
        )

    async def change_scenario(id: ScenarioId, action: str) -> CallToolResult:
        state = ScenarioState.model_validate(
            await call("POST", f"/api/scenarios/{id}/{action}")
        )
        return _result(
            f"{state.id} ({state.title}) is now {'active' if state.active else 'inactive'}.",
            state,
        )

    @server.tool(annotations=WRITES)
    async def inject_scenario(
        id: Annotated[
            ScenarioId, Field(description="Scenario id from list_scenarios.")
        ],
    ) -> CallToolResult:
        """Start a demo scenario in the data source and return its new state. Use it
        only when the user asks for a demo."""
        return await change_scenario(id, "inject")

    @server.tool(annotations=WRITES)
    async def clear_scenario(
        id: Annotated[
            ScenarioId, Field(description="Scenario id from list_scenarios.")
        ],
    ) -> CallToolResult:
        """Stop a demo scenario in the data source and return its new state. Use it
        to end a demo the user started."""
        return await change_scenario(id, "clear")

    return server


def main() -> None:
    settings = load_settings()
    server = create_server(f"http://127.0.0.1:{settings.ports.core}")
    uvicorn.run(
        server.streamable_http_app(),
        host="127.0.0.1",
        port=settings.ports.mcp,
        access_log=False,
        log_config=None,
    )
