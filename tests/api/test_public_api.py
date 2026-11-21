import asyncio
from collections.abc import Iterator
from contextlib import aclosing, closing
from datetime import datetime
from typing import Any

import pytest
from factories import (
    make_add_metric_result,
    make_decisions,
    make_graph,
    make_incident_s7,
    make_metrics,
    make_trail_s7,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from eye.api.app import MetricDefinition, TruthRow, create_app
from eye.config import Settings
from eye.contracts import (
    AddMetricRequest,
    AddMetricResult,
    Edge,
    Event,
    GraphView,
    Incident,
    MetricDef,
    ScenarioId,
    ScenarioState,
    SentinelDecision,
    Series,
    Symptom,
    TrailStep,
)
from eye.incidents.store import IncidentStore
from eye.llm.client import LLMClient
from eye.orchestrator import HOUR, Orchestrator, Status, StepSink
from eye.registry.engine import MetricEngine
from eye.registry.loader import Registry
from eye.sim.store import Store


class World:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.states: list[ScenarioState] = []

    def step(self) -> None:
        self.now += HOUR

    async def run(
        self, ts: datetime, *, open_incidents: list[Incident], on_step: StepSink
    ) -> list[SentinelDecision]:
        return make_decisions()

    def timeline(self) -> list[ScenarioState]:
        return self.states.copy()

    def inject(self, id: ScenarioId) -> ScenarioState:
        state = ScenarioState(
            id=id, title="Outage", active=True, injected_ts=self.now, cleared_ts=None
        )
        self.states.append(state)
        return state

    def clear(self, id: ScenarioId) -> None:
        state = next(state for state in self.states if state.id == id)
        state.active = False
        state.cleared_ts = self.now


class Graph:
    def __init__(self, metric: MetricDef) -> None:
        self.metric = metric
        self.graph = make_graph()

    def metrics(self) -> list[MetricDef]:
        return [self.metric]

    def view(self, node: str | None, depth: int | None) -> GraphView:
        return self.graph

    def in_edges(self, node: str) -> list[Edge]:
        return [edge for edge in self.graph.edges if edge.dst == node]

    def out_edges(self, node: str) -> list[Edge]:
        return [edge for edge in self.graph.edges if edge.src == node]


@pytest.fixture
def service() -> Iterator[tuple[FastAPI, Settings, Orchestrator]]:
    settings = Settings(sim={"autostart": False, "seconds_per_sim_hour": 0.5})
    sim = World(settings.sim.start_ts)
    metric = make_metrics()[1].model_copy(
        update={
            "sql": "SELECT ts, country, platform, sessions AS num FROM traffic_hourly"
        }
    )
    registry = Registry(
        {metric.name: metric},
        {metric.name: ("traffic_hourly",)},
        {metric.name: False},
        [],
        [],
    )

    async def add(
        request: AddMetricRequest, clean_hours: list[datetime], on_step: StepSink
    ) -> AddMetricResult:
        result = make_add_metric_result()
        assert request.name == result.metric.name
        assert sim.now not in clean_hours and sim.now - HOUR in clean_hours
        return result

    with (
        closing(Store(":memory:")) as db,
        db.cursor() as connection,
        closing(IncidentStore(":memory:")) as incidents,
    ):
        connection.execute(
            "INSERT INTO traffic_hourly VALUES (?, 'DE', 'web', 112, 50, 52, 10)",
            [sim.now],
        )
        connection.execute(
            """INSERT INTO scenario_log VALUES ('scn_s7', ?, 'S7', 'inject', '{"enabled":true}')""",
            [sim.now],
        )
        sample = make_incident_s7()
        incidents._save(sample)
        for step in make_trail_s7():
            incidents.add_step(step)
        orchestrator = Orchestrator(
            settings,
            sim,
            sim,
            incidents,
            LLMClient(settings.llm),
            lambda *_: 9,
            scenarios=sim,
            scenario_titles={"S7": "Outage"},
            add_metric=add,
        )
        yield (
            create_app(
                orchestrator,
                connection,
                registry,
                Graph(metric),
                MetricEngine(connection, registry),
            ),
            settings,
            orchestrator,
        )


def test_public_api_contracts(service: tuple[FastAPI, Settings, Orchestrator]) -> None:
    app, settings, _ = service
    with TestClient(app) as client:

        def get(path: str, **params: Any) -> Any:
            response = client.get(path, params=params)
            assert response.status_code == 200, response.text
            return response.json()

        def post(path: str, model: type[BaseModel], **kwargs: Any) -> Any:
            response = client.post(path, **kwargs)
            assert response.status_code == 200, response.text
            return model.model_validate(response.json())

        assert not Status.model_validate(get("/api/status")).running
        assert GraphView.model_validate(
            get("/api/graph", node="sessions", depth=0)
        ).nodes
        (metric,) = get("/api/metrics")
        assert "sql" not in metric
        MetricDef.model_validate(metric)
        assert MetricDefinition.model_validate(
            get("/api/metrics/sessions/definition")
        ).sql
        start = settings.sim.start_ts
        window = {
            "start": start.isoformat(),
            "end": (start + HOUR).isoformat(),
            "segment": '{"country":"DE"}',
        }
        series = Series.model_validate(get("/api/metrics/sessions", **window))
        assert series.points[0].value == 112
        assert (
            client.get(
                "/data/metric", params={**window, "name": "sessions"}
            ).status_code
            == 200
        )
        assert post("/api/clock/step", Status).tick == 1
        post(
            "/api/metrics",
            AddMetricResult,
            json={
                "name": "orders_per_session",
                "description": "Orders per session",
                "expression": "orders / sessions",
            },
        )
        assert all(
            SentinelDecision.model_validate(item) for item in get("/api/anomalies")
        )
        assert get("/api/anomalies", since="2027-01-01T00:00:00Z") == []
        (item,) = get("/api/incidents", status="open")
        sample = Incident.model_validate(item)
        assert not sample.hypotheses
        assert Incident.model_validate(get(f"/api/incidents/{sample.id}")).hypotheses
        assert all(
            TrailStep.model_validate(step)
            for step in get(f"/api/incidents/{sample.id}/trail")
        )
        assert not ScenarioState.model_validate(get("/api/scenarios")[0]).active
        assert post("/api/scenarios/S7/inject", ScenarioState).active
        assert post("/api/clock/pause", Status).paused
        assert post("/api/clock/resume", Status).running
        assert not post("/api/scenarios/S7/clear", ScenarioState).active
        assert client.get("/api/demo/truth").status_code == 404
        settings.demo.show_truth = True
        assert TruthRow.model_validate(get("/api/demo/truth")[0]).params == {
            "enabled": True
        }
        assert client.get("/").status_code == 200
        assert client.get("/web/app.js").status_code == 200
        assert client.get("/api/metrics/no_such_metric").status_code == 404


def test_incident_symptoms(service: tuple[FastAPI, Settings, Orchestrator]) -> None:
    app, _, orchestrator = service
    item = orchestrator.incidents.all()[0]
    item.symptoms = [Symptom(node="orders", first_change_ts=item.entry.ts)]
    orchestrator.incidents._save(item)
    expected = [symptom.model_dump(mode="json") for symptom in item.symptoms]
    with TestClient(app) as client:
        assert client.get(f"/api/incidents/{item.id}").json()["symptoms"] == expected
        assert client.get("/api/incidents").json()[0]["symptoms"] == expected


@pytest.mark.asyncio
async def test_sse_tick_and_heartbeat(
    service: tuple[FastAPI, Settings, Orchestrator],
) -> None:
    app, settings, orchestrator = service
    subscribed = asyncio.Event()
    messages: asyncio.Queue[dict] = asyncio.Queue()
    disconnect = asyncio.Event()

    async def receive() -> dict:
        if not subscribed.is_set():
            subscribed.set()
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    request = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "method": "GET",
        "path": "/api/events",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "http_version": "1.1",
        "server": ("127.0.0.1", 8000),
    }
    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app(request, receive, messages.put))
        try:
            response = await messages.get()
            assert response["status"] == 200
            assert b"text/event-stream" in dict(response["headers"])[b"content-type"]
            orchestrator.resume()
            chunk = await asyncio.wait_for(
                messages.get(), settings.sim.seconds_per_sim_hour * 2
            )
            assert chunk["body"].startswith(b"event: tick\ndata: ")
            tick = Event.model_validate_json(chunk["body"].split(b"data: ", 1)[1])
            assert tick.type == "tick" and tick.payload == {"tick": 1}
        finally:
            disconnect.set()
            await task
        async with aclosing(orchestrator.events.stream(0.01)) as stream:
            assert await anext(stream) == ": heartbeat\n\n"
