import asyncio
import json
import shutil
import threading
from collections.abc import Iterator
from contextlib import aclosing, closing
from datetime import datetime
from pathlib import Path
from secrets import token_urlsafe
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

from eye.api.app import (
    IncidentDetail,
    MetricDefinition,
    UIStatus,
    create_app,
    human_actor,
)
from eye.config import Settings
from eye.contracts import (
    AddMetricResult,
    Edge,
    Event,
    GraphView,
    Incident,
    MetricDef,
    SellerAlert,
    Series,
    Symptom,
    TrailStep,
)
from eye.contracts_v2 import AgentResult, IncidentV2
from eye.directory import AgentDirectory
from eye.incidents.store import IncidentStore
from eye.orchestrator import HOUR, Orchestrator, Status, TenantBackpressure
from eye.registry.engine import MetricEngine
from eye.registry.loader import Registry
from eye.sim.store import Store
from selleros.data import FileSellerStore
from selleros.server import create_app as create_selleros_app


class World:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.live_start = start
        self.history_start = start - 28 * 24 * HOUR

    def step(self) -> None:
        self.now += HOUR


def test_metric_backpressure_returns_429(service, monkeypatch) -> None:
    app, _, core = service

    async def full(request):
        raise TenantBackpressure("Tenant default queued backlog is full")

    monkeypatch.setattr(core, "register_metric", full)
    with TestClient(app) as client:
        response = client.post(
            "/api/metrics",
            json={
                "name": "orders_per_session",
                "description": "Orders per session",
                "expression": "orders / sessions",
            },
        )
    assert response.status_code == 429
    assert "backlog is full" in response.json()["detail"]


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


def test_ui_reads_v2_lifecycle_and_sanitized_exchange(service) -> None:
    app, _, core = service
    sample = make_incident_s7()
    with TestClient(app) as client:
        status = UIStatus.model_validate(client.get("/api/ui/status").json())
        assert status.sim_ts == core.status().sim_ts
        rows = client.get("/api/ui/incidents").json()
        assert len(rows) == 1
        listed = IncidentV2.model_validate(rows[0])
        assert listed.root_cause is None and not listed.hypotheses
        detail = IncidentDetail.model_validate(
            client.get(f"/api/ui/incidents/{sample.id}").json()
        )
        assert detail.incident.root_cause == sample.root_cause
        assert detail.incident.tenant_id == "default"
        assert client.get("/api/ui/incidents/unknown").status_code == 404
        sample.status = "resolved"
        sample.resolved_ts = sample.updated_ts
        core.incidents._save(sample)
        assert client.get("/api/ui/incidents").json()[0]["status"] == "recovered"


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_seller_revert_requires_exact_human_approval(
    service: tuple[FastAPI, Settings, Orchestrator],
    repo_root: Path,
    tmp_path: Path,
    stale: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_token, operator_token = token_urlsafe(), token_urlsafe()
    monkeypatch.setenv("EYE_SELLER_REVERT_SERVICE_TOKEN", service_token)
    monkeypatch.setenv("EYE_SELLER_REVERT_OPERATOR_TOKEN", operator_token)
    monkeypatch.delenv("EYE_TRUST_PROXY_PERSON_HEADER", raising=False)
    seller_client = {"Authorization": f"Bearer {service_token}"}
    operator = {"Authorization": f"Bearer {operator_token}"}
    api, settings, core = service
    snapshot_path = tmp_path / "sellers.json"
    shutil.copyfile(
        repo_root / "packages/selleros/tests/fixtures/s8.json", snapshot_path
    )
    store = FileSellerStore(tmp_path)
    transport = httpx.ASGITransport(app=api)
    core.directory = AgentDirectory(
        settings.a2a,
        transport=httpx.ASGITransport(
            app=create_selleros_app(
                store,
                approvals_url="http://orchestrator",
                approvals_transport=transport,
            )
        ),
    )
    alert = SellerAlert(
        incident_id=core.incidents.all()[0].id,
        seller_ids=["S-14"],
        skus=["E02", "E04"],
        summary="S8 manual price change",
        recommended_action="revert manual price",
    )
    journal = tmp_path / "actions.jsonl"
    async with httpx.AsyncClient(
        transport=transport, base_url="http://orchestrator"
    ) as client:
        for _ in range(2):
            ack = await core.directory.notify_seller_os(alert)
            assert ack.received and ack.actions_taken == []
        assert not journal.exists()
        assert (await client.get("/api/approvals", headers=seller_client)).json() == []
        approvals = (await client.get("/api/approvals", headers=operator)).json()
        assert len(approvals) == 2
        assert all(row["status"] == "pending" for row in approvals)
        by_sku = {row["args"]["sku"]: row["id"] for row in approvals}
        approve_url = f"/api/approvals/{by_sku['E02']}"
        permission_url = f"/api/approvals/seller-revert/{by_sku['E02']}/permission"
        bogus = dict(approvals[0]["args"], incident_id="not-the-approved-incident")
        for untrusted in ({}, {"X-Eye-Person": "reviewer"}, operator):
            assert (
                await client.put(
                    "/api/approvals/seller-revert", json=bogus, headers=untrusted
                )
            ).status_code == 401
        for untrusted in ({}, {"X-Eye-Person": "reviewer"}, seller_client):
            assert (
                await client.post(
                    approve_url, json={"approve": True}, headers=untrusted
                )
            ).status_code == 401
        assert len((await client.get("/api/approvals", headers=operator)).json()) == 2
        assert (
            await client.get(permission_url, headers=seller_client)
        ).status_code == 409
        assert not journal.exists()
        (
            await client.post(
                f"/api/approvals/{by_sku['E04']}",
                json={"approve": False},
                headers=operator,
            )
        ).raise_for_status()
        for stop_url in (
            "/api/kill",
            "/api/tenants/default/kill",
            f"/api/runs/selleros:{alert.incident_id}/kill",
        ):
            spoofed = {"X-Eye-Person": "reviewer"}
            assert (await client.post(stop_url, headers=spoofed)).status_code == 401
            (await client.post(stop_url, headers=operator)).raise_for_status()
            assert (await client.delete(stop_url, headers=spoofed)).status_code == 401
            assert (
                await client.post(approve_url, json={"approve": True}, headers=operator)
            ).status_code == 409
            assert core.control.approval(by_sku["E02"])["status"] == "pending"
            assert not (await core.directory.notify_seller_os(alert)).received
            assert not journal.exists()
            (await client.delete(stop_url, headers=operator)).raise_for_status()
        if stale:
            snapshot = store.read()
            latest = next(
                p
                for p in snapshot.sellers[0].price_history
                if p.sku == "E02" and p.source == "manual"
            )
            latest.price += 1
            snapshot_path.write_text(snapshot.model_dump_json())
        (
            await client.post(approve_url, json={"approve": True}, headers=operator)
        ).raise_for_status()
        assert (await client.get(permission_url, headers=operator)).status_code == 401
        assert (
            await client.get(permission_url, headers=seller_client)
        ).status_code == 204
        (await client.post("/api/kill", headers=operator)).raise_for_status()
        assert (
            await client.get(permission_url, headers=seller_client)
        ).status_code == 409
        (await client.delete("/api/kill", headers=operator)).raise_for_status()
        with monkeypatch.context() as env:
            env.setenv("EYE_TRUST_PROXY_PERSON_HEADER", "1")
            proxy = {"X-Eye-Person": "proxy-verified-reviewer"}
            (await client.post("/api/kill", headers=proxy)).raise_for_status()
            (await client.delete("/api/kill", headers=proxy)).raise_for_status()
        if stale:
            assert not journal.exists()
        else:
            records = journal.read_text().splitlines()
            assert len(records) == 1
            assert json.loads(records[0])["idempotency_key"] == by_sku["E02"]
            assert json.loads(records[0])["restore_price"] == 99
        assert (await core.directory.notify_seller_os(alert)).actions_taken == []
        if not stale:
            assert journal.read_text().splitlines() == records
        else:
            assert not journal.exists()


def test_scenario_controls_are_not_public(
    service: tuple[FastAPI, Settings, Orchestrator],
) -> None:
    app, _settings, _orchestrator = service
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
        assert client.get("/api/anomalies").status_code == 404
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
        assert client.get("/").status_code == 404
        assert client.get("/web/app.js").status_code == 404
        assert client.get("/app.js").status_code == 404
        assert client.get("/styles.css").status_code == 404
        assert client.post("/api/chat", json={"messages": []}).status_code == 404
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
    person: str | None,
    header: str | None,
    expected: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EYE_TRUST_PROXY_PERSON_HEADER", "1")
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


def test_human_control_routes(
    service: tuple[FastAPI, Settings, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EYE_TRUST_PROXY_PERSON_HEADER", "1")
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
