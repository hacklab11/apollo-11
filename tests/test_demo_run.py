import importlib.util
import json
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from factories import make_incident_s7, make_status

from eye.agents.sentinel import Sentinel
from eye.api.app import create_app
from eye.config import Settings
from eye.contracts import Hypothesis, InvestigationResult
from eye.graph.store import GraphStore
from eye.incidents.store import IncidentStore
from eye.llm import client as llm_client
from eye.llm.client import LLMClient
from eye.orchestrator import Orchestrator
from eye.registry.engine import MetricEngine
from eye.registry.loader import load_registry
from eye.sim.engine import Simulator
from eye.sim.scenarios import ScenarioManager
from eye.sim.source import SimSource

SPEC = importlib.util.spec_from_file_location(
    "demo_run", Path(__file__).resolve().parents[1] / "scripts/demo_run.py"
)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.mark.asyncio
async def test_demo_i3_hypothesis_does_not_swallow_s8(tmp_path, monkeypatch):
    settings = Settings(
        sim={"autostart": False},
        llm={
            "provider": "openai_compatible",
            "api_key_env": "OFFLINE_DEMO_KEY",
            "models": {"default": "deepseek/deepseek-v4.1-flash"},
        },
    )
    monkeypatch.setenv("OFFLINE_DEMO_KEY", "offline-dummy")
    segment = {"category": "electronics", "seller_tier": "small_3P"}
    provider_calls = []

    async def provider(request):
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        context = json.loads(content.split("\n\nRuntime budget:")[0])
        provider_calls.append(context["ts"])
        decisions = [
            {
                "anomaly": candidate["anomaly"],
                "action": "investigate",
                "reason": "Investigate the measured electronics seller anomaly",
            }
            for candidate in context["candidates"]
            if candidate["anomaly"]["metric"] == "gmv"
            and candidate["anomaly"]["segment"] == segment
        ]
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "submit",
                                    "type": "function",
                                    "function": {
                                        "name": "submit",
                                        "arguments": json.dumps(
                                            {"decisions": decisions}
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 17000, "completion_tokens": 100},
            },
        )

    investigations = []

    async def investigate(entry, on_step):
        investigations.append(entry)
        s8 = entry.ts.hour >= 10
        root = Hypothesis(
            id=f"hyp_{entry.ts.hour}",
            statement="S8 price typo" if s8 else "I3 Electronics coupon day",
            cause_type="price_change" if s8 else "planned_event",
            root_node="gmv",
            segment=segment,
            first_change_ts=settings.sim.start_ts + timedelta(hours=10 if s8 else 0),
            explained_share=0.8,
            status="supported",
        )
        return InvestigationResult(
            investigation_id="scripted",
            entry=entry,
            verdict="HYPOTHESIS",
            summary=root.statement,
            root_cause=root,
            hypotheses=[root],
            contributions=[],
            open_questions=[],
            suggested_actions=[],
            steps_used=1,
            stop_reason="budget",
        )

    async def narrative(result):
        return result.summary, result.summary

    with (
        closing(Simulator(settings, db_path=":memory:")) as sim,
        closing(IncidentStore(":memory:", reporter=narrative)) as store,
    ):
        sim.selleros_export.path = tmp_path / "sellers.json"
        sim.backfill()
        manager = ScenarioManager(sim)
        source = SimSource(sim, manager, {"I3": "Coupons", "S8": "Price typo"})
        with sim.store.cursor() as con:
            registry = load_registry(con)
            graph = GraphStore(path=None)
            engine = MetricEngine(con, registry)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(provider)
            ) as provider_client:
                llm = LLMClient(settings.llm, http_client=provider_client)
                monkeypatch.setattr(llm_client, "_client", llm)
                core = Orchestrator(
                    settings,
                    source,
                    Sentinel(con, registry, llm=settings.llm),
                    store,
                    llm,
                    lambda *_: 9,
                    scenarios=manager,
                    scenario_titles=source.scenario_titles,
                    investigate=investigate,
                    invalidate=engine.invalidate,
                )
                app = create_app(core, con, registry, graph, engine)
                try:
                    async with httpx.AsyncClient(
                        base_url="http://offline",
                        transport=httpx.ASGITransport(app=app),
                    ) as client:
                        await runner.prepare(client, core.status())
                        coupon = store.all()[0]
                        assert coupon.verdict == "HYPOTHESIS"
                        assert coupon.root_cause.cause_type == "planned_event"
                        await runner.step(client, 0)
                        injected = (
                            await runner.inject_scenario(client, "S8")
                        ).injected_ts
                        assert injected.hour == 10
                        await runner.step(client, 0)
                        assert any(
                            d.action == "defer" and d.anomaly.segment == segment
                            for d in core.decisions
                            if d.anomaly.ts == injected
                        )
                        incident = None
                        for _ in range(runner.WAIT_HOURS):
                            await runner.step(client, 0)
                            incident = await runner.find_incident(client, injected)
                            if incident:
                                break
                        assert incident is not None, (
                            "S8 investigate decisions were swallowed by the I3 incident; "
                            f"investigated hours: {[a.ts.hour for a in investigations]}"
                        )
                        assert incident.id == coupon.id
                        assert incident.summary == "S8 price typo"
                        assert incident.entry.ts >= injected
                        assert incident.updated_ts >= injected
                        assert incident.root_cause.first_change_ts == injected
                        assert len(set(incident.investigation_ids)) >= 2
                        assert llm.terminal_error() is None
                        assert len(provider_calls) <= 19
                finally:
                    await core.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_find_incident_explains_electronics_rejections(empty):
    incident = make_incident_s7()
    incident.entry.segment = {"category": "electronics"}
    incident.root_cause.segment = {"category": "electronics"}
    incident.root_cause.cause_type = "planned_event"
    incident.status = "persisting"
    injected = incident.entry.ts + timedelta(hours=7)

    def respond(request):
        return httpx.Response(
            200,
            json=([] if empty else [incident.model_dump(mode="json")])
            if request.url.path == "/api/incidents"
            else incident.model_dump(mode="json"),
        )

    async with httpx.AsyncClient(
        base_url="http://offline", transport=httpx.MockTransport(respond)
    ) as client:
        rejected = []
        assert await runner.find_incident(client, injected, rejected=rejected) is None
        if empty:
            assert rejected == []
        else:
            assert len(rejected) == 1
            assert incident.id in rejected[0]
            assert "predates S8" in rejected[0]
            assert "cause=planned_event" in rejected[0]
            assert "first_change=" in rejected[0]


@pytest.mark.asyncio
async def test_find_incident_diagnostics_propagate_http_errors():
    async with httpx.AsyncClient(
        base_url="http://offline",
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await runner.find_incident(client, Settings().sim.start_ts, rejected=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_demo_stop_reports_rejected_incidents(empty, monkeypatch):
    incident = make_incident_s7()
    incident.entry.segment = {"category": "electronics"}
    incident.root_cause.segment = {"category": "electronics"}
    incident.root_cause.cause_type = "planned_event"
    status = runner.Status.model_validate(make_status())
    status.sim_ts = status.sim_ts.replace(hour=9)
    injected = status.sim_ts + timedelta(hours=1)
    state = runner.ScenarioState(
        id="S8", title="Price typo", active=True, injected_ts=injected, cleared_ts=None
    )
    monkeypatch.setattr(runner, "prepare", AsyncMock())
    monkeypatch.setattr(runner, "step", AsyncMock(return_value=status))
    monkeypatch.setattr(runner, "inject_scenario", AsyncMock(return_value=state))
    monkeypatch.setattr(runner, "decisions", AsyncMock(return_value=[]))

    def respond(request):
        if request.url.path in {"/api/status", "/api/clock/pause"}:
            body = status.model_dump(mode="json")
        elif request.url.path == "/api/incidents":
            body = [] if empty else [incident.model_dump(mode="json")]
        else:
            body = incident.model_dump(mode="json")
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        base_url="http://offline", transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(RuntimeError) as stopped:
            await runner.demo(client, 0)
    message = str(stopped.value)
    assert "no open electronics price-change incident for S8" in message
    if empty:
        assert "no electronics incidents found" in message
    else:
        assert incident.id in message and "cause=planned_event" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["before_s8", "after_s8", "unavailable"])
async def test_runner_enriches_read_timeout_without_hiding_original_error(
    state, monkeypatch
):
    def respond(request):
        if state == "unavailable":
            return httpx.Response(503)
        if request.url.path == "/api/scenarios":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "S8" if state == "after_s8" else "I3",
                        "title": "Scenario",
                        "active": True,
                        "injected_ts": Settings().sim.start_ts.isoformat(),
                        "cleared_ts": None,
                    }
                ],
            )
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(
        base_url="http://offline", transport=httpx.MockTransport(respond)
    )
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(
        runner, "demo", AsyncMock(side_effect=httpx.ReadTimeout("slow step"))
    )
    with pytest.raises(RuntimeError) as stopped:
        await runner.run(SimpleNamespace(base_url="http://offline", speed=0))
    message = str(stopped.value)
    assert "ReadTimeout" in message
    assert {
        "before_s8": "S8 not injected; active scenarios=I3",
        "after_s8": "no electronics incidents found",
        "unavailable": "incident diagnostics unavailable",
    }[state] in message
