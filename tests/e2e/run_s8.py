"""Drive a fresh, paused Compose stack through I3@0/S8@10 via Core's API."""

import time
from datetime import timedelta
from typing import Any

import httpx
from pydantic import TypeAdapter

from eye.contracts import Incident, ScenarioState, SentinelDecision, TrailStep
from eye.orchestrator import Status

INCIDENTS = TypeAdapter(list[Incident])
TRAIL = TypeAdapter(list[TrailStep])
DECISIONS = TypeAdapter(list[SentinelDecision])
REPORT_TITLE = "Electronics price-change investigation"


def request(client: httpx.Client, method: str, path: str) -> httpx.Response:
    return client.request(method, path).raise_for_status()


def step(client: httpx.Client) -> Status:
    status = Status.model_validate_json(
        request(client, "POST", "/api/clock/step").content
    )
    assert not status.llm_stop_reason, status
    print(f"OK tick {status.tick}: {status.sim_ts.isoformat()}", flush=True)
    return status


def reported(client: httpx.Client, injected: ScenarioState) -> Incident | None:
    for item in INCIDENTS.validate_json(
        request(client, "GET", "/api/incidents").content
    ):
        if item.entry.ts < injected.injected_ts:
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
        for actor in ("detective", "skeptic", "reporter"):
            assert any(s.actor == actor and s.action == "llm_call" for s in trail), (
                actor
            )
        for actor in ("detective", "skeptic"):
            for name in ("baseline_compare", "check_freshness", "submit"):
                assert any(
                    s.actor == actor and s.tool == name and s.output is not None
                    for s in trail
                ), (actor, name)
        assert all(e.step_id for e in detail.root_cause.evidence_for), detail
        return detail
    return None


def run() -> None:
    with (
        httpx.Client(base_url="http://127.0.0.1:8000", timeout=120) as core,
        httpx.Client(base_url="http://scripted-model:8400", timeout=5) as model,
    ):
        request(model, "GET", "/healthz")
        status = Status.model_validate_json(request(core, "GET", "/api/status").content)
        assert status.tick == 0 and status.paused, "Use a fresh stack and volumes"
        assert request(core, "GET", "/api/incidents").json() == []
        deadline = time.monotonic() + 30
        while not request(model, "GET", "/stats").json()["calls"].get("cartographer"):
            assert time.monotonic() < deadline, "Cartographer bootstrap timed out"
            time.sleep(0.5)
        i3 = ScenarioState.model_validate_json(
            request(core, "POST", "/api/scenarios/I3/inject").content
        )
        assert i3.active
        for _ in range(10):
            step(core)
        assert request(core, "GET", "/api/incidents").json() == []
        injected = ScenarioState.model_validate_json(
            request(core, "POST", "/api/scenarios/S8/inject").content
        )
        assert injected.active and injected.injected_ts == i3.injected_ts + timedelta(
            hours=10
        )
        print(f"OK S8 injected at {injected.injected_ts.isoformat()}", flush=True)

        step(core)
        decisions = DECISIONS.validate_json(
            request(core, "GET", "/api/anomalies").content
        )
        assert any(
            d.action == "defer" and d.anomaly.ts == injected.injected_ts
            for d in decisions
        ), "S8's incomplete first hour must be deferred"
        assert request(core, "GET", "/api/incidents").json() == []

        step(core)
        deadline = time.monotonic() + 60
        while (incident := reported(core, injected)) is None:
            assert time.monotonic() < deadline, (
                "S8 never reached investigated/reported state"
            )
            time.sleep(0.5)
        stats: dict[str, Any] = request(model, "GET", "/stats").json()
        assert not stats["errors"], stats
        for actor in ("cartographer", "sentinel", "detective", "skeptic", "reporter"):
            assert stats["calls"].get(actor, 0) > 0, stats
        print(
            f"OK S8 incident {incident.id}: status={incident.status}, "
            f"verdict={incident.verdict}, report={incident.title!r}",
            flush=True,
        )
        print(
            f"OK offline model calls: {stats['calls']}; unexpected requests: 0",
            flush=True,
        )
        print(
            "Compose S8 e2e passed (injection -> deferred hour -> investigation -> report).",
            flush=True,
        )


if __name__ == "__main__":
    run()
