import asyncio
import threading
from collections.abc import Iterator
from contextlib import aclosing, closing
from datetime import datetime
from typing import Any

import httpx
import pytest
from factories import (
    make_add_metric_result,
    make_graph,
    make_incident_s7,
    make_metrics,
    make_trail_s7,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from scripted_agents import ScriptedAgents

from eye.api.app import MetricDefinition, create_app, human_actor
from eye.config import Settings
from eye.contracts import (
    AddMetricResult,
    Edge,
    Event,
    GraphView,
    Incident,
    MetricDef,
    SentinelDecision,
    Series,
    Symptom,
    TrailStep,
)
from eye.contracts_v2 import AgentResult
from eye.incidents.store import IncidentStore
from eye.orchestrator import HOUR, Orchestrator, Status
from eye.registry.engine import MetricEngine
from eye.registry.loader import Registry
from eye.sim.store import Store


class World:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.live_start = start
        self.history_start = start - 28 * 24 * HOUR

    def step(self) -> None:
        self.now += HOUR


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
def service(
    request: pytest.FixtureRequest,
    tmp_path,
) -> Iterator[tuple[FastAPI, Settings, Orchestrator]]:
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

    scripted = ScriptedAgents(
        {"map": AgentResult(status="done", result=make_add_metric_result())}
    )

    with (
        closing(Store(":memory:")) as db,
        db.cursor() as connection,
        closing(IncidentStore(":memory:")) as incidents,
    ):
        connection.execute(
            "INSERT INTO traffic_hourly VALUES (?, 'DE', 'web', 112, 50, 52, 10)",
            [sim.now],
        )
        sample = make_incident_s7()
        incidents._save(sample)
        for step in make_trail_s7():
            incidents.add_step(step)
        orchestrator = Orchestrator(
            settings,
            sim,
            incidents,
            lambda *_: 9,
            directory=scripted.directory(settings.a2a),
            database_url=f"sqlite:///{tmp_path}/control.sqlite",
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


def test_scenario_controls_are_not_public(
    service: tuple[FastAPI, Settings, Orchestrator],
) -> None:
    app, settings, _orchestrator = service
    settings.demo.show_truth = True
    with TestClient(app) as client:
        assert client.get("/api/demo/truth").status_code == 404
        assert client.get("/api/scenarios").status_code == 404
        assert "active_scenarios" not in client.get("/api/status").json()
        assert client.post("/api/scenarios/S8/inject").status_code == 404
        assert client.post("/api/scenarios/S8/clear").status_code == 404


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
        assert post("/api/clock/pause", Status).paused
        assert post("/api/clock/resume", Status).running
        assert client.get("/api/demo/truth").status_code == 404
        settings.demo.show_truth = True
        assert client.get("/api/demo/truth").status_code == 404
        assert client.get("/").status_code == 200
        assert client.get("/web/app.js").status_code == 200
        assert client.get("/app.js").status_code == 200
        assert client.get("/styles.css").status_code == 200
        for path in (
            "/mock/status.json",
            "/web/mock/status.json",
            "/web/README.md",
        ):
            assert client.get(path).status_code == 404
        assert client.get("/api/metrics/no_such_metric").status_code == 404


def test_llm_mode_cannot_change_at_runtime(
    service: tuple[FastAPI, Settings, Orchestrator],
) -> None:
    app, settings, _ = service
    with TestClient(app) as client:
        response = client.post("/api/llm/mode", json={"mode": "replay"})
        status = Status.model_validate(client.get("/api/status").json())
    assert response.status_code in {404, 405}
    assert "/api/llm/mode" not in {getattr(route, "path", "") for route in app.routes}
    assert status.llm_mode == settings.llm.mode == "live"


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
async def test_live_clock_step_returns_before_analysis(
    service: tuple[FastAPI, Settings, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, settings, orchestrator = service
    scanning = threading.Event()
    finish_scan = threading.Event()
    original = orchestrator.directory.call

    async def run(*args: Any, **kwargs: Any):
        scanning.set()
        await asyncio.to_thread(finish_scan.wait)
        return await original(*args, **kwargs)

    monkeypatch.setattr(orchestrator.directory, "call", run)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        url = "/api/clock/step?wait_for_analysis=false"
        request = asyncio.create_task(client.post(url))
        try:
            await asyncio.wait_for(asyncio.to_thread(scanning.wait), 10)
            current = Status.model_validate((await client.get("/api/status")).json())
            assert current.tick == 1
            response = await asyncio.wait_for(asyncio.shield(request), 2)
            assert response.status_code == 200
            status = Status.model_validate(response.json())
            assert status.tick == 1
            assert status.sim_ts == settings.sim.start_ts
            assert status.paused and not status.running
            busy = await asyncio.wait_for(client.post(url), 2)
            assert busy.status_code == 409
        finally:
            finish_scan.set()
            await request


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


@pytest.mark.parametrize(
    "status", ["open", "persisting", "resolved", "innocent", "deferred"]
)
def test_legacy_incident_statuses(
    service: tuple[FastAPI, Settings, Orchestrator], status: str
) -> None:
    app, _, core = service
    item = core.incidents.all()[0]
    item.status = status
    core.incidents._save(item)
    with TestClient(app) as client:
        response = client.get("/api/incidents", params={"status": status})
        assert response.status_code == 200, response.text
        assert Incident.model_validate(response.json()[0]).status == status
        detail = client.get(f"/api/incidents/{item.id}")
        assert Incident.model_validate(detail.json()).status == status


@pytest.mark.parametrize(
    ("person", "header", "expected"),
    [
        (None, None, None),
        (None, "", None),
        (None, "   ", None),
        (None, "proxy-reviewer", "proxy-reviewer"),
        ("middleware-reviewer", None, "middleware-reviewer"),
        ("middleware-reviewer", "proxy-reviewer", "middleware-reviewer"),
    ],
)
def test_human_actor(
    person: str | None, header: str | None, expected: str | None
) -> None:
    request = Request(
        {
            "type": "http",
            "headers": [] if header is None else [(b"x-eye-person", header.encode())],
        }
    )
    if person is not None:
        request.state.person = person
    if expected is None:
        with pytest.raises(HTTPException) as exc:
            human_actor(request)
        assert exc.value.status_code == 401
    else:
        assert human_actor(request) == expected


def test_human_control_routes(service: tuple[FastAPI, Settings, Orchestrator]) -> None:
    app, _, core = service
    incident = core.incidents.all()[0]
    with TestClient(app) as client:
        close_url = f"/api/incidents/{incident.id}/close"
        body = {"tenant_id": "default", "note": "Reviewed recovery"}
        assert client.post(close_url, json=body).status_code == 401
        assert client.post("/api/kill").status_code == 401
        assert (
            client.post("/api/approvals/unknown", json={"approve": True}).status_code
            == 401
        )
        client.headers["X-Eye-Person"] = "reviewer"
        assert client.post(close_url, json=body).status_code == 409
        incident.status = "resolved"
        core.incidents._save(incident)
        closed = client.post(close_url, json=body)
        assert closed.status_code == 200
        assert closed.json()["closure"]["closed_by"] == "reviewer"
        assert client.post(close_url, json=body).status_code == 409
        identifier = core.control.pending(
            "run", incident.id, "human_input", {"question": "Continue?"}
        )
        assert client.get("/api/approvals").json()[0]["id"] == identifier
        assert (
            client.post(
                f"/api/approvals/{identifier}", json={"approve": False}
            ).status_code
            == 200
        )
        assert client.get("/api/approvals").json()[0]["status"] == "rejected"
        assert client.get("/api/approvals").json()[0]["decided_by"] == "reviewer"
        for endpoint in (
            "/api/kill",
            "/api/tenants/default/kill",
            "/api/runs/run/kill",
        ):
            assert client.post(endpoint).json() == {"active": True}
            assert client.delete(endpoint).json() == {"active": False}
        audit = client.get("/api/audit").json()
        assert any(
            row["action"] == "incident_closed" and row["actor"] == "reviewer"
            for row in audit
        )
