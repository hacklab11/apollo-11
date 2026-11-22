"""Exercise S8 investigation, recovery and human closure through Core."""

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import TypeAdapter

from eye.contracts import Incident, TrailStep
from eye.contracts_v2 import DEFAULT_TENANT, IncidentCloseRequest, IncidentV2
from eye.orchestrator import Status
from eye.sim.scenarios import ScenarioState
from eye.sim.service import Status as SimStatus

INCIDENTS = TypeAdapter(list[Incident])
TRAIL = TypeAdapter(list[TrailStep])
SCENARIOS = TypeAdapter(list[ScenarioState])
REPORT_TITLE = "Electronics price-change investigation"


def request(
    client: httpx.Client, method: str, path: str, **kwargs: Any
) -> httpx.Response:
    return client.request(method, path, **kwargs).raise_for_status()


def step(client: httpx.Client, last_tick: int) -> Status:
    status = Status.model_validate_json(
        request(client, "POST", "/api/clock/step").content
    )
    assert not status.llm_stop_reason, status
    if status.tick != last_tick:
        print(
            f"OK Core consumed tick {status.tick}: {status.sim_ts.isoformat()}",
            flush=True,
        )
    time.sleep(0.5)
    return status


def reported(client: httpx.Client, typo_ts: datetime) -> Incident | None:
    for item in INCIDENTS.validate_json(
        request(client, "GET", "/api/incidents").content
    ):
        if item.entry.ts < typo_ts:
            raise AssertionError(f"Incident predates S8: {item.id}")
        if item.status not in {"open", "persisting"} or item.title != REPORT_TITLE:
            continue
        detail = Incident.model_validate_json(
            request(client, "GET", f"/api/incidents/{item.id}").content
        )
        assert detail.verdict == "CONFIRMED", detail
        assert detail.root_cause and detail.root_cause.cause_type == "price_change"
        assert detail.investigation_ids and not detail.open_questions, detail
        trail = TRAIL.validate_json(
            request(client, "GET", f"/api/incidents/{item.id}/trail").content
        )
        for actor in ("detective", "skeptic"):
            assert any(s.actor == actor and s.action == "llm_call" for s in trail), (
                actor
            )
        for actor in ("detective", "skeptic"):
            for name in ("baseline_compare", "check_freshness", "submit"):
                assert any(
                    s.actor == actor
                    and s.tool == name
                    and s.output_summary
                    and s.output is None
                    and s.input == {}
                    for s in trail
                ), (actor, name)
        assert all(e.step_id for e in detail.root_cause.evidence_for), detail
        assert any(
            row["agent"] == "report"
            and row["action"] == "agent_done"
            and row["incident_id"] == item.id
            for row in request(client, "GET", "/api/audit").json()
        ), "Reporter skill must complete for this incident"
        return detail
    return None


def run() -> None:
    with (
        httpx.Client(base_url="http://127.0.0.1:8000", timeout=120) as core,
        httpx.Client(base_url="http://scripted-model:8400", timeout=5) as model,
        httpx.Client(base_url="http://sim:8100", timeout=30) as sim,
    ):
        request(model, "GET", "/healthz")
        status = Status.model_validate_json(request(core, "GET", "/api/status").content)
        assert status.tick == 0 and status.paused, "Use a fresh stack and volumes"
        assert request(core, "GET", "/api/incidents").json() == []
        sim_status = SimStatus.model_validate_json(
            request(sim, "GET", "/status").content
        )
        assert not sim_status.active_scenarios, "Use a fresh simulator volume"
        timeline = SCENARIOS.validate_json(
            request(sim, "POST", "/scenarios/s8").content
        )
        injected = next(s for s in timeline if s.id == "S8" and s.active)
        assert injected.injected_ts is not None
        print(f"OK S8 injected at {injected.injected_ts.isoformat()}", flush=True)
        delay = yaml.safe_load(Path("config/scenarios.yaml").read_text())["S8"][
            "typo_delay_hours"
        ]
        typo_ts = injected.injected_ts + timedelta(hours=delay)
        model.post(
            "/scenario", json={"investigate_after": typo_ts.isoformat()}
        ).raise_for_status()
        deadline = time.monotonic() + 90
        while (incident := reported(core, typo_ts)) is None:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "S8 never reached investigated/reported state; "
                    f"Core={request(core, 'GET', '/api/status').text}; "
                    f"sim={request(sim, 'GET', '/status').text}; "
                    f"model={request(model, 'GET', '/stats').text}"
                )
            status = step(core, status.tick)
        print(
            f"OK S8 incident {incident.id}: status={incident.status}, "
            f"verdict={incident.verdict}, report={incident.title!r}",
            flush=True,
        )
        close_path = f"/api/incidents/{incident.id}/close"
        person = "offline-operator"
        identity = {"X-Eye-Person": person}
        close_body = IncidentCloseRequest(
            tenant_id=DEFAULT_TENANT, note="Reviewed measured S8 recovery."
        ).model_dump(mode="json")
        early = core.post(close_path, headers=identity, json=close_body)
        assert early.status_code == 409, early.text
        print("OK human close rejected before recovery (409)", flush=True)
        timeline = SCENARIOS.validate_json(
            request(sim, "DELETE", "/scenarios/s8").content
        )
        assert any(s.id == "S8" and not s.active for s in timeline), timeline
        print("OK S8 cleared through sim; waiting for measured recovery", flush=True)
        deadline = time.monotonic() + 120
        while incident.status != "resolved":
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"S8 never recovered; incident={incident.model_dump_json()}; "
                    f"Core={request(core, 'GET', '/api/status').text}; "
                    f"sim={request(sim, 'GET', '/status').text}"
                )
            status = step(core, status.tick)
            incident = Incident.model_validate_json(
                request(core, "GET", f"/api/incidents/{incident.id}").content
            )
        assert incident.resolved_ts is not None
        print(
            f"OK S8 incident recovered (legacy status=resolved): {incident.resolved_ts.isoformat()}",
            flush=True,
        )
        unauthenticated = core.post(close_path, json=close_body)
        assert unauthenticated.status_code == 401, unauthenticated.text
        print("OK human close requires identity (401)", flush=True)
        closed = IncidentV2.model_validate_json(
            request(core, "POST", close_path, headers=identity, json=close_body).content
        )
        assert closed.status == "closed" and closed.recovered_ts == incident.resolved_ts
        assert closed.closure is not None and closed.closure.closed_by == person
        assert closed.closure.note == close_body["note"]
        assert any(
            row["action"] == "incident_closed"
            and row["incident_id"] == incident.id
            and row["actor"] == person
            for row in request(core, "GET", "/api/audit").json()
        ), "Human closure must be audited"
        print(f"OK S8 incident closed by {person}: {closed.id} (audited)", flush=True)
        stats: dict[str, Any] = request(model, "GET", "/stats").json()
        assert not stats["errors"], stats
        for actor in ("sentinel", "detective", "skeptic", "reporter"):
            assert stats["calls"].get(actor, 0) > 0, stats
        print(
            f"OK offline model calls: {stats['calls']}; unexpected requests: 0",
            flush=True,
        )
        print(
            "Compose S8 e2e passed (3 Detectives; sim injection -> eye-data -> investigation -> report -> recovered -> human close).",
            flush=True,
        )


if __name__ == "__main__":
    run()
