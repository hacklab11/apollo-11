import asyncio
import json
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb
import httpx
import pytest
from factories import make_anomaly, make_incident_s7, make_trail_s7

from eye.agents.reporter import Reporter
from eye.config import Settings
from eye.contracts import (
    Anomaly,
    Event,
    Incident,
    InvestigationResult,
    SentinelDecision,
)
from eye.datasource import DuckDBSource
from eye.incidents.store import IncidentStore
from eye.llm.client import LLMClient, LLMUnavailable
from eye.main import _reporter
from eye.orchestrator import HOUR, Orchestrator, StepSink
from eye.schema import create_tables
from eye.warehouse.client import DataUnavailable


@pytest.mark.asyncio
async def test_duckdb_ticks_only_on_complete_ingestion(tmp_path: Path) -> None:
    settings = Settings(sim={"autostart": False})
    ts = settings.sim.start_ts
    partitions = tmp_path / "partitions.parquet"
    path = tmp_path / "warehouse.duckdb"

    def publish(loaded: int) -> None:
        with duckdb.connect() as con:
            con.execute(
                "COPY (SELECT 'traffic_hourly' AS table_name, 'DE' AS country, "
                "?::TIMESTAMPTZ AS ts, 10::BIGINT AS expected_rows, "
                "10::BIGINT AS loaded_rows, ?::TIMESTAMPTZ AS loaded_at "
                "UNION ALL SELECT 'traffic_hourly', 'DE', ?, 10, ?, ?) TO ? (FORMAT PARQUET)",
                [ts, ts + HOUR, ts + HOUR, loaded, ts + 2 * HOUR, str(partitions)],
            )

    publish(9)
    with duckdb.connect(str(path)) as con:
        create_tables(con)
        con.execute("DROP TABLE partition_status")
        con.execute(
            f"CREATE VIEW partition_status AS SELECT * FROM read_parquet('{partitions}')"
        )
    source = DuckDBSource(path)
    store = IncidentStore(":memory:")
    core = Orchestrator(
        settings, source, Sentinel(0), store, LLMClient(settings.llm), lambda *_: 0
    )
    calls: list[datetime] = []
    events: asyncio.Queue[Event] = asyncio.Queue()
    core.events._subscribers.add(events)
    original = core.sentinel.run

    async def run(stamp: datetime, **kwargs) -> list[SentinelDecision]:
        calls.append(stamp)
        return await original(stamp, **kwargs)

    core.sentinel.run = run
    try:
        assert core.sim_ts == source.live_start
        assert source.history_start == ts
        assert (await core.step_once()).tick == 0
        assert calls == [] and events.empty()
        publish(10)
        assert (await core.step_once()).tick == 1
        assert calls == [ts + HOUR]
        assert events.get_nowait().type == "tick"
        assert events.empty()
        assert (await core.step_once()).tick == 1
        assert calls == [ts + HOUR] and events.empty()
    finally:
        await core.close()
        store.close()
        source.close()


@pytest.mark.asyncio
async def test_reporter_budget_exhaustion_still_finishes_incident() -> None:
    settings = Settings(sim={"autostart": False})
    llm = LLMClient(settings.llm)
    result: InvestigationResult | None = None

    async def investigate(anomaly: Anomaly, on_step: StepSink) -> InvestigationResult:
        nonlocal result
        llm._calls.extend([time.monotonic()] * settings.llm.max_calls_per_hour)
        result = InvestigationResult(
            investigation_id="inv_fake",
            entry=anomaly,
            verdict="HYPOTHESIS",
            summary="No confirmed cause",
            root_cause=None,
            hypotheses=[],
            contributions=[],
            open_questions=[],
            suggested_actions=[],
            steps_used=0,
            stop_reason="budget",
        )
        return result

    store = IncidentStore(":memory:")
    orchestrator = Orchestrator(
        settings,
        Clock(settings.sim.start_ts),
        Sentinel(1),
        store,
        llm,
        lambda *_: 9,
        investigate=investigate,
    )
    store.reporter = _reporter(llm, settings, orchestrator).write
    try:
        await orchestrator.step_once()
        assert result is not None and llm.status()["budget_paused"]
        (incident,) = store.all()
        assert incident.status == "open"
        assert (incident.title, incident.summary) == Reporter.template(result)
    finally:
        await orchestrator.close()
        store.close()


@dataclass
class Clock:
    now: datetime

    def __post_init__(self) -> None:
        self.live_start = self.now
        self.history_start = self.now - 28 * 24 * HOUR

    def step(self) -> None:
        self.now += HOUR


class Sentinel:
    def __init__(self, count: int) -> None:
        self.count = count

    async def run(
        self, ts: datetime, *, open_incidents: list[Incident], on_step: StepSink
    ) -> list[SentinelDecision]:
        decisions = [
            SentinelDecision(
                anomaly=make_anomaly().model_copy(
                    update={
                        "ts": ts,
                        "id": f"anm_{i}",
                        "segment": {"seller_tier": str(i)},
                    }
                ),
                action="investigate",
                reason="Outage",
            )
            for i in range(self.count)
        ]
        self.count = 0
        return decisions


@pytest.mark.asyncio
async def test_replays_incident_lifecycle_after_warehouse_outage() -> None:
    settings = Settings(sim={"autostart": False})
    clock = Clock(settings.sim.start_ts + HOUR)
    readings = []

    def lookup(_name: str, _segment: dict[str, str], ts: datetime) -> float:
        readings.append(ts)
        if len(readings) == 1:
            raise DataUnavailable("warehouse unavailable")
        return 5

    with closing(IncidentStore(":memory:")) as store:
        incident = await store.upsert(
            InvestigationResult(
                investigation_id="inv_retry",
                entry=make_anomaly().model_copy(update={"ts": clock.now - HOUR}),
                verdict="HYPOTHESIS",
                summary="No confirmed cause",
                root_cause=None,
                hypotheses=[],
                contributions=[],
                open_questions=[],
                suggested_actions=[],
                steps_used=0,
                stop_reason="budget",
            )
        )
        core = Orchestrator(
            settings, clock, Sentinel(0), store, LLMClient(settings.llm), lookup
        )
        try:
            first = clock.now
            assert (await core.step_once()).tick == 1
            assert (await core.step_once()).tick == 2
            assert readings == [first, first, first + HOUR]
            updated = store.get(incident.id)
            assert updated.status == "persisting"
            assert updated.impact_eur_total == 2 * incident.impact_eur_per_hour
        finally:
            await core.close()


@pytest.mark.asyncio
async def test_failed_sentinel_logs_reason_and_scans_next_hour(caplog):
    settings = Settings(sim={"autostart": False})
    seen = []

    class UnavailableSentinel:
        async def run(self, ts, **kwargs):
            seen.append(ts)
            raise LLMUnavailable("Chat Completions HTTP 429: Free model quota exceeded")

    store = IncidentStore(":memory:")
    core = Orchestrator(
        settings,
        Clock(settings.sim.start_ts),
        UnavailableSentinel(),
        store,
        LLMClient(settings.llm),
        lambda *_: 9,
    )
    try:
        await core.step_once()
        await core.step_once()
        assert seen == [settings.sim.start_ts, settings.sim.start_ts + HOUR]
        assert store.all() == []
        failures = [
            json.loads(record.message)
            for record in caplog.records
            if '"event": "sentinel_failed"' in record.message
        ]
        assert len(failures) == 2
        assert all(
            "HTTP 429: Free model quota exceeded" in failure["detail"]
            for failure in failures
        )
    finally:
        await core.close()
        store.close()


@pytest.mark.asyncio
async def test_daily_quota_stops_sentinel_on_following_ticks(monkeypatch, caplog):
    settings = Settings(sim={"autostart": False}, llm={"provider": "openai_compatible"})
    monkeypatch.setenv(settings.llm.api_key_env, "offline-placeholder")
    scans = []

    def provider(request):
        return httpx.Response(
            429,
            json={"error": {"message": "Rate limit exceeded: free-models-per-day."}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        llm = LLMClient(settings.llm, http_client=http)

        class QuotaLimitedSentinel:
            async def run(self, ts, **kwargs):
                scans.append(ts)
                await llm.create(
                    {
                        "model": "test",
                        "max_tokens": 10,
                        "system": [],
                        "tools": [],
                        "messages": [],
                    }
                )
                return []

        store = IncidentStore(":memory:")
        core = Orchestrator(
            settings,
            Clock(settings.sim.start_ts),
            QuotaLimitedSentinel(),
            store,
            llm,
            lambda *_: 9,
        )
        try:
            first = await core.step_once()
            second = await core.step_once()
            assert scans == [settings.sim.start_ts]
            assert second.tick == 2
            assert second.llm_stop_reason == first.llm_stop_reason
            assert "Daily model request quota exhausted" in second.llm_stop_reason
            assert (
                sum(
                    '"event": "llm_daily_quota_exhausted"' in r.message
                    for r in caplog.records
                )
                == 1
            )
        finally:
            await core.close()
            store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [True, False])
async def test_start_retries_interrupted_investigation_with_its_trail(
    tmp_path, manual: bool
) -> None:
    settings = Settings(sim={"autostart": False})
    decision = SentinelDecision(
        anomaly=make_anomaly(), action="investigate", reason="Outage"
    )
    path = tmp_path / "ops.duckdb"
    first_process = IncidentStore(path)
    incident = first_process.match(decision)
    assert incident is not None
    incident.investigation_ids = ["inv_interrupted"]
    first_process._save(incident)
    previous_step = make_trail_s7()[0].model_copy(
        update={"investigation_id": "inv_interrupted", "n": 1}
    )
    first_process.add_step(previous_step)
    first_process.close()

    async def investigate(anomaly: Anomaly, on_step: StepSink) -> InvestigationResult:
        assert orchestrator.sim.now == incident.entry.ts + HOUR
        await on_step(previous_step.model_copy(update={"id": "stp_retried"}))
        return InvestigationResult(
            investigation_id="inv_fake",
            entry=anomaly,
            verdict="HYPOTHESIS",
            summary="No confirmed cause",
            root_cause=None,
            hypotheses=[],
            contributions=[],
            open_questions=[],
            suggested_actions=[],
            steps_used=1,
            stop_reason="budget",
        )

    store = IncidentStore(path)
    orchestrator = Orchestrator(
        settings,
        Clock(incident.entry.ts + HOUR),
        Sentinel(0),
        store,
        LLMClient(settings.llm),
        lambda *_: 9,
        investigate=investigate,
    )
    try:
        await orchestrator.start()
        status = await orchestrator._step(manual=manual)
        if not manual:
            assert status.tick == 0 and orchestrator.paused
            assert status.investigations_running == 1
            await asyncio.gather(*orchestrator._tasks)
        (final,) = store.all()
        assert final.id == incident.id and final.status == "open"
        assert final.investigation_ids == ["inv_interrupted"]
        assert [(step.id, step.n) for step in store.trail("inv_interrupted")] == [
            (previous_step.id, 1),
            ("stp_retried", 2),
        ]
    finally:
        await orchestrator.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_step_investigates_upserts_and_resolves_after_recovery(
    failure: bool,
) -> None:
    settings = Settings(sim={"autostart": False})
    clock = Clock(settings.sim.start_ts)
    anomalous = True
    template = make_incident_s7()

    async def investigate(anomaly: Anomaly, on_step: StepSink) -> InvestigationResult:
        await on_step(make_trail_s7()[0])
        if failure:
            raise RuntimeError("must not appear in the event")
        return InvestigationResult(
            investigation_id="inv_fake",
            entry=anomaly,
            verdict="CONFIRMED",
            summary=template.summary,
            root_cause=template.root_cause,
            hypotheses=template.hypotheses,
            contributions=template.contributions,
            open_questions=[],
            suggested_actions=[],
            steps_used=1,
            stop_reason="confirmed",
        )

    store = IncidentStore(":memory:")
    orchestrator = Orchestrator(
        settings,
        clock,
        Sentinel(1),
        store,
        LLMClient(settings.llm),
        lambda *_: 9 if anomalous else 0,
        investigate=investigate,
    )
    stream = orchestrator.events.stream()
    subscription = asyncio.create_task(anext(stream))
    try:
        await asyncio.sleep(0)
        first_anomalous_hour = clock.now
        status = await orchestrator.step_once()
        assert (
            status.tick == 1
            and not status.running
            and status.investigations_running == 0
        )
        (incident,) = store.all()
        assert incident.status == "open"
        assert incident.verdict == ("HYPOTHESIS" if failure else "CONFIRMED")
        if failure:
            assert incident.summary == "investigation failed: RuntimeError"
        (investigation_id,) = incident.investigation_ids
        assert len(store.trail(investigation_id)) == 1
        events = [await subscription] + [await anext(stream) for _ in range(7)]
        assert [
            Event.model_validate_json(wire.split("data: ", 1)[1]).type
            for wire in events
        ] == [
            "tick",
            "anomaly",
            "sentinel_decision",
            "incident_upserted",
            "investigation_started",
            "trail_step",
            "investigation_finished",
            "incident_upserted",
        ]
        anomalous = False
        recovered_hour = clock.now
        for _ in range(settings.reporter.resolve_after_ticks):
            await orchestrator.step_once()
        (incident,) = store.all()
        assert incident.status == "resolved"
        assert incident.resolved_ts == orchestrator.sim_ts
        assert first_anomalous_hour not in orchestrator.clean_hours()
        assert recovered_hour not in orchestrator.clean_hours()
        assert incident.resolved_ts in orchestrator.clean_hours()
    finally:
        await stream.aclose()
        await orchestrator.close()
        store.close()


@pytest.mark.asyncio
async def test_clock_pauses_for_investigations_and_dispatches_within_limit() -> None:
    settings = Settings(sim={"seconds_per_sim_hour": 0.5})
    clock = Clock(settings.sim.start_ts)
    started = asyncio.Event()
    release = asyncio.Event()
    store = IncidentStore(":memory:")
    count = 0
    active = 0
    peak = 0

    async def investigate(anomaly: Anomaly, on_step: StepSink) -> InvestigationResult:
        nonlocal count, active, peak
        count += 1
        active += 1
        peak = max(peak, active)
        if active == 2:
            started.set()
        await release.wait()
        active -= 1
        return InvestigationResult(
            investigation_id="inv_fake",
            entry=anomaly,
            verdict="HYPOTHESIS",
            summary="Insufficient evidence",
            root_cause=None,
            hypotheses=[],
            contributions=[],
            open_questions=[],
            suggested_actions=[],
            steps_used=0,
            stop_reason="budget",
        )

    orchestrator = Orchestrator(
        settings,
        clock,
        Sentinel(3),
        store,
        LLMClient(settings.llm),
        lambda *_: 3,
        investigate=investigate,
    )
    try:
        await orchestrator.start()
        await asyncio.wait_for(started.wait(), 2)
        assert orchestrator.status().investigations_running == 2
        await asyncio.sleep(0.6)
        assert orchestrator.status().tick == 1
        orchestrator.pause()
        release.set()
        status = await orchestrator.step_once()
        assert status.tick == 2 and count == 3 and peak == 2
    finally:
        release.set()
        await orchestrator.close()
        store.close()


@pytest.mark.asyncio
async def test_matching_during_reporter_preserves_occurrence_and_last_seen() -> None:
    settings = Settings(orchestrator={"pause_sim_during_investigation": False})
    reporting = asyncio.Event()
    release = asyncio.Event()
    sentinel = Sentinel(1)

    async def reporter(result: InvestigationResult) -> tuple[str, str]:
        reporting.set()
        await release.wait()
        return Reporter.template(result)

    async def investigate(anomaly: Anomaly, on_step: StepSink) -> InvestigationResult:
        return InvestigationResult(
            investigation_id="inv_fake",
            entry=anomaly,
            verdict="HYPOTHESIS",
            summary="No confirmed cause",
            root_cause=None,
            hypotheses=[],
            contributions=[],
            open_questions=[],
            suggested_actions=[],
            steps_used=0,
            stop_reason="budget",
        )

    store = IncidentStore(":memory:", reporter=reporter)
    orchestrator = Orchestrator(
        settings,
        Clock(settings.sim.start_ts),
        sentinel,
        store,
        LLMClient(settings.llm),
        lambda *_: 9,
        investigate=investigate,
    )
    try:
        await orchestrator._step(manual=False)
        await asyncio.wait_for(reporting.wait(), 2)
        sentinel.count = 1
        await orchestrator._step(manual=False)
        (pending,) = store.all()
        assert pending.status == "investigating" and pending.occurrences == 2
        assert pending.last_seen_ts == settings.sim.start_ts + HOUR
        release.set()
        await orchestrator.close()
        (final,) = store.all()
        assert final.status == "open"
        assert final.occurrences == 2
        assert final.last_seen_ts == pending.last_seen_ts
        assert final.updated_ts >= pending.last_seen_ts
    finally:
        release.set()
        await orchestrator.close()
        store.close()
