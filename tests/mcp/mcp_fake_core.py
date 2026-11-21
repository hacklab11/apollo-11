import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

MOCK = Path(__file__).resolve().parents[1] / "fixtures" / "web"


def mock(name: str) -> object:
    return json.loads((MOCK / name).read_text())


Calls = list[tuple[str, dict[str, str]]]


def fake_core(calls: Calls, scenario_list: object = None) -> FastAPI:
    """A tiny core API that serves the frozen tests/fixtures/web payloads."""
    app = FastAPI()

    @app.middleware("http")
    async def record(request: Request, call_next):  # type: ignore[no-untyped-def]
        calls.append((request.url.path, dict(request.query_params)))
        return await call_next(request)

    @app.get("/api/status")
    async def status() -> object:
        return mock("status.json")

    @app.get("/api/incidents")
    async def incidents(status: str | None = None) -> object:
        items = mock("incidents.json")
        assert isinstance(items, list)
        return [item for item in items if status in (None, item["status"])]

    @app.get("/api/incidents/{id}")
    async def incident(id: str) -> object:
        if id != "inc_s7":
            raise HTTPException(404, f"incident {id} not found")
        return mock("incident_s7.json")

    @app.get("/api/incidents/{id}/trail")
    async def trail(id: str) -> object:
        return mock("trail_s7.json")

    @app.get("/api/graph")
    async def graph() -> object:
        return mock("graph.json")

    @app.get("/api/metrics/{name}")
    async def series(name: str) -> object:
        return mock("metric_orders.json")

    @app.get("/api/metrics/{name}/definition")
    async def definition(name: str) -> object:
        return mock("metric_def_orders.json")

    @app.post("/api/metrics")
    async def add_metric() -> object:
        return mock("add_metric_result.json")

    @app.get("/api/scenarios")
    async def scenarios() -> object:
        return mock("scenarios.json") if scenario_list is None else scenario_list

    @app.post("/api/scenarios/{id}/{action}")
    async def change(id: str, action: str) -> object:
        active = action == "inject"
        ts = "2026-11-27T14:00:00Z"
        return {
            "id": id,
            "title": f"Scenario {id}",
            "active": active,
            "injected_ts": ts if active else None,
            "cleared_ts": None if active else ts,
        }

    return app
