from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from factories import make_anomaly, make_incident_s7, make_trail_s7
from scripted_agents import ScriptedAgents

from eye.config import Settings
from eye.contracts import InvestigationResult, SentinelDecision
from eye.contracts_v2 import (
    AgentResult,
    DetectRequest,
    DetectResult,
    IncidentClosure,
    InvestigateResult,
    ReportResult,
    RunUsageSummary,
    TaskMetadata,
)
from eye.control import audit_hash
from eye.incidents.store import IncidentStore
from eye.orchestrator import HOUR, Orchestrator
from eye.warehouse.client import DataUnavailable


class Clock:
    def __init__(self) -> None:
        self.now = self.live_start = datetime(2026, 11, 27, tzinfo=UTC)
        self.history_start = self.now - 28 * 24 * HOUR

    def step(self) -> None:
        self.now += HOUR


@pytest.mark.asyncio
async def test_s8_workflow(tmp_path: Path) -> None:
    clock = Clock()
    anomaly = make_anomaly().model_copy(update={"ts": clock.now, "metric": "gmv"})
    investigation = InvestigationResult(
        investigation_id="inv_s8",
        entry=anomaly,
        verdict="HYPOTHESIS",
        summary="S8 price feedback",
        root_cause=None,
        hypotheses=[],
        contributions=[],
        open_questions=[],
        suggested_actions=[],
        steps_used=1,
        stop_reason="budget",
    )
    trail = [make_trail_s7()[0].model_copy(update={"investigation_id": "inv_s8"})]
    scripted = ScriptedAgents(
        {
            "detect": AgentResult(
                status="done",
                result=DetectResult(
                    decisions=[
                        SentinelDecision(
                            anomaly=anomaly, action="investigate", reason="S8"
                        )
                    ]
                ),
                usage=RunUsageSummary(input_tokens=10, cost_usd=0.01),
            ),
            "investigate": AgentResult(
                status="done",
                result=InvestigateResult(investigation=investigation, trail=trail),
            ),
            "report": AgentResult(
                status="done",
                result=ReportResult(
                    title="S8 headphones", summary="Price feedback investigated"
                ),
            ),
        }
    )
    settings = Settings(sim={"autostart": False})
    store = IncidentStore(":memory:")
    orchestrator = Orchestrator(
        settings,
        clock,
        store,
        lambda *_: 0,
        directory=scripted.directory(settings.a2a),
        database_url=f"sqlite:///{tmp_path}/control.sqlite",
    )
    await orchestrator.start()
    try:
        await orchestrator.step_once()
        (item,) = store.all()
        assert (item.title, item.summary) == (
            "S8 headphones",
            "Price feedback investigated",
        )
        assert store.trail("inv_s8") == trail
        assert len(scripted.requests) == 3
        for _, wire, header in scripted.requests:
            metadata = TaskMetadata.model_validate(wire["params"]["metadata"])
            assert metadata.tenant_id == "default"
            assert metadata.traceparent == header
            assert wire["params"]["message"]["contextId"] == metadata.context_id
            assert len(wire["params"]["message"]["parts"]) == 1
        assert (
            scripted.requests[1][1]["params"]["metadata"]["budget"]["tokens"]
            == settings.orchestrator.run_tokens - 10
        )
        with pytest.raises(ValueError, match="recovered"):
            store.human_close(
                item.id,
                IncidentClosure(
                    tenant_id="default",
                    closed_by="person",
                    note="Reviewed",
                    closed_ts=datetime.now(UTC),
                ),
            )
        for offset in range(1, settings.reporter.resolve_after_ticks + 1):
            store.lifecycle(clock.live_start + offset * HOUR, lambda *_: 0)
        closed = store.human_close(
            item.id,
            IncidentClosure(
                tenant_id="default",
                closed_by="person",
                note="Reviewed",
                closed_ts=datetime.now(UTC),
            ),
        )
        assert closed.status == "closed"
        orchestrator.control.set_kill("system", "default", True, "person")
        await orchestrator.step_once()
        assert len(scripted.requests) == 3
        assert orchestrator.control.audit_rows()[-1]["action"] == "run_killed"
        previous = "0" * 64
        for row in orchestrator.control.audit_rows():
            assert row.pop("prev_hash") == previous
            digest = row.pop("hash")
            row.pop("id")
            assert digest == audit_hash(previous, row)
            previous = digest
    finally:
        await orchestrator.close()
        store.close()


@pytest.mark.asyncio
async def test_directory_unknown_skill() -> None:
    settings = Settings()
    scripted = ScriptedAgents()
    directory = scripted.directory(settings.a2a)
    await directory.resolve()
    orchestrator_metadata = TaskMetadata(
        tenant_id="default",
        context_id="test",
        traceparent="00-" + "1" * 32 + "-" + "2" * 16 + "-01",
        budget={"tokens": 100, "cost_usd": 0.5, "deadline": "2099-01-01T00:00:00Z"},
    )
    with pytest.raises(ValueError, match="unknown A2A skill: missing"):
        await directory.call(
            "missing",
            DetectRequest(ts=datetime.now(UTC)),
            orchestrator_metadata,
            DetectResult,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["needs_input", "failed", "gave_up", "empty"])
async def test_non_success_and_empty_detection(tmp_path: Path, state: str) -> None:
    clock = Clock()
    settings = Settings(sim={"autostart": False})
    if state == "empty":
        result = AgentResult(status="done", result=DetectResult(decisions=[]))
    elif state == "needs_input":
        result = AgentResult(status=state, question="Review source freshness?")
    else:
        result = AgentResult(status=state, reason="No usable evidence")
    scripted = ScriptedAgents({"detect": result})
    store = IncidentStore(":memory:")
    core = Orchestrator(
        settings,
        clock,
        store,
        lambda *_: 0,
        directory=scripted.directory(settings.a2a),
        database_url=f"sqlite:///{tmp_path}/control.sqlite",
    )
    await core.start()
    try:
        await core.step_once()
        assert store.all() == []
        assert len(scripted.requests) == 1
        assert bool(core.control.approval_rows()) == (state == "needs_input")
        assert (
            core.control.audit_rows()[0]["action"]
            == f"agent_{'done' if state == 'empty' else state}"
        )
        await core.step_once()
        assert (
            scripted.requests[-1][1]["params"]["message"]["parts"][0]["data"][
                "previous_decisions"
            ]
            == []
        )
    finally:
        await core.close()
        store.close()


@pytest_asyncio.fixture
async def workflow(tmp_path: Path):
    settings = Settings(sim={"autostart": False})
    scripted = ScriptedAgents()
    store = IncidentStore(":memory:")
    core = Orchestrator(
        settings,
        Clock(),
        store,
        lambda *_: 0,
        directory=scripted.directory(settings.a2a),
        database_url=f"sqlite:///{tmp_path}/control.sqlite",
    )
    await core.start()
    try:
        yield core, scripted
    finally:
        await core.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_incomplete_investigation_leaves_pending_state(
    workflow, exhausted: bool
) -> None:
    core, scripted = workflow
    decision = SentinelDecision(
        anomaly=make_anomaly().model_copy(update={"ts": core.sim.now}),
        action="investigate",
        reason="Investigate regression",
    )
    scripted.executors["detect"].result = AgentResult(
        status="done",
        result=DetectResult(decisions=[decision]),
        usage=RunUsageSummary(input_tokens=core.settings.orchestrator.run_tokens)
        if exhausted
        else RunUsageSummary(),
    )
    await core.step_once()
    (incident,) = core.incidents.all()
    assert incident.status == "open"
    assert incident.verdict == "HYPOTHESIS"
    scripted.executors["detect"].result = AgentResult(
        status="done", result=DetectResult(decisions=[])
    )
    for _ in range(core.incidents.settings.resolve_after_ticks):
        await core.step_once()
    assert core.incidents.get(incident.id).status == "resolved"


@pytest.mark.asyncio
async def test_lifecycle_retries_the_same_hour_after_unavailable_data(workflow) -> None:
    core, _ = workflow
    item = make_incident_s7()
    item.status = "open"
    item.opened_ts = core.sim.now - HOUR
    core.incidents._save(item)
    attempts = []

    def reading(metric, segment, ts):
        attempts.append(ts)
        if len(attempts) == 1:
            raise DataUnavailable("temporary warehouse outage")
        return 0

    core.z_lookup = reading
    ts = core.sim.now
    await core.step_once()
    assert attempts == [ts, ts]
    assert core.incidents.get(item.id).normal_ticks == 1
