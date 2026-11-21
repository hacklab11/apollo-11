from datetime import timedelta

import pytest
from factories import make_anomaly, make_incident_s7, make_trail_s7

from eye.agents.reporter import Reporter
from eye.contracts import InvestigationResult, SellerAlertAck, SentinelDecision
from eye.incidents.store import IncidentStore, canonical, fingerprint


def result(name: str = "a", *, hours: int = 0) -> InvestigationResult:
    example = make_incident_s7()
    entry = example.entry.model_copy(
        update={"id": name, "ts": example.entry.ts + timedelta(hours=hours)}
    )
    return InvestigationResult(
        investigation_id=name,
        entry=entry,
        verdict="CONFIRMED",
        summary=example.summary,
        root_cause=example.root_cause,
        hypotheses=example.hypotheses,
        contributions=example.contributions,
        open_questions=[],
        suggested_actions=example.suggested_actions,
        steps_used=5,
        stop_reason="confirmed",
    )


@pytest.fixture
def store(tmp_path):
    value = IncidentStore(tmp_path / "ops.duckdb")
    yield value
    value.close()


def test_canonical_and_fingerprint():
    assert canonical({"platform": "web", "country": "DE"}) == "country=DE,platform=web"
    assert canonical({}) == ""
    assert fingerprint("orders", {}) == "orders||pending"
    assert (
        fingerprint("orders", {"country": "DE"}, "release")
        == "orders|country=DE|release"
    )


def test_match_provisional_and_empty(store):
    decision = SentinelDecision(anomaly=make_anomaly(), action="watch", reason="test")
    assert store.match(decision) is None
    assert store.all() == []
    decision.action = "investigate"
    first = store.match(decision)
    assert first.status == "investigating"
    decision.anomaly = decision.anomaly.model_copy(
        update={"ts": decision.anomaly.ts + timedelta(hours=1)}
    )
    attached = store.match(decision)
    assert attached.id == first.id
    assert attached.occurrences == 2
    assert attached.last_seen_ts == decision.anomaly.ts


@pytest.mark.asyncio
async def test_graph_match_overlap(store):
    incident = await store.upsert(result())
    decision = SentinelDecision(
        anomaly=make_anomaly().model_copy(
            update={"metric": "gmv", "segment": {"platform": "web"}}
        ),
        action="investigate",
        reason="test",
    )
    graph = lambda src, dst: (src, dst) == ("cart_to_checkout_rate", "gmv")
    assert store.match(decision, graph).id == incident.id
    decision.anomaly.segment = {"platform": "ios"}
    assert store.match(decision, graph).id != incident.id


@pytest.mark.asyncio
async def test_same_final_fingerprint_merges_older(store):
    first = await store.upsert(result())
    second = await store.upsert(result("b", hours=1))
    assert second.id == first.id
    assert second.investigation_ids == ["a", "b"]
    assert len(store.all()) == 2
    assert sum(item.merged_into == first.id for item in store.all()) == 1


@pytest.mark.asyncio
async def test_absorbed_snapshot_is_not_merged_twice(store):
    first = await store.upsert(result())
    store.lifecycle(first.entry.ts + timedelta(hours=1), lambda *_: 5)
    value = result("b", hours=2)
    value.entry.metric = "gmv"
    pending = store.match(
        SentinelDecision(anomaly=value.entry, action="investigate", reason="New metric")
    )
    pending.impact_eur_total = 10
    pending.notifications = [{"to": "selleros", "ok": True}]
    store._save(pending)
    merged = await store.upsert(value)
    assert merged.id == first.id
    assert merged.occurrences == 2
    assert merged.impact_eur_total == first.impact_eur_per_hour + 10
    assert merged.notifications == pending.notifications
    assert store.get(pending.id).merged_into == first.id


@pytest.mark.asyncio
async def test_same_fingerprint_keeps_latest_findings(store):
    preliminary = result()
    preliminary.verdict = "HYPOTHESIS"
    preliminary.stop_reason = "budget"
    preliminary.root_cause = preliminary.root_cause.model_copy(
        update={"status": "open", "evidence_for": []}
    )
    preliminary.hypotheses = [preliminary.root_cause]
    first = await store.upsert(preliminary)
    store.lifecycle(first.entry.ts + timedelta(hours=1), lambda *_: 5)
    confirmed = result("confirmed", hours=2)
    confirmed.entry.impact_eur_per_hour = 123456
    confirmed.open_questions = ["Latest question"]
    confirmed.suggested_actions = ["Latest action"]
    confirmed.trigger = confirmed.root_cause.id

    async def report(value):
        return "Confirmed findings", "Latest investigation narrative."

    store.reporter = report
    survivor = await store.upsert(confirmed)
    assert survivor.id == first.id
    assert survivor.opened_ts == first.opened_ts
    assert survivor.verdict == "CONFIRMED"
    assert survivor.title == "Confirmed findings"
    assert survivor.summary == "Latest investigation narrative."
    assert survivor.root_cause == confirmed.root_cause
    assert survivor.hypotheses == confirmed.hypotheses
    assert survivor.contributions == confirmed.contributions
    assert survivor.entry == confirmed.entry
    assert survivor.impact_eur_per_hour == 123456
    assert survivor.impact_eur_total == first.impact_eur_per_hour
    assert survivor.open_questions == confirmed.open_questions
    assert survivor.suggested_actions == confirmed.suggested_actions
    assert (
        store.db.execute(
            "SELECT trigger_id FROM incidents WHERE id = ?", [survivor.id]
        ).fetchone()[0]
        == confirmed.trigger
    )


@pytest.mark.asyncio
async def test_same_trigger_merges(store):
    left = result()
    left.trigger = "trigger"
    first = await store.upsert(left)
    right = result("b", hours=1)
    right.trigger = "trigger"
    right.root_cause = right.root_cause.model_copy(update={"root_node": "orders"})
    merged = await store.upsert(right)
    assert merged.id == first.id
    assert merged.investigation_ids == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_arrives_first", [True, False])
async def test_trigger_incident_kept_even_when_newer(store, trigger_arrives_first):
    trigger = result("trigger")
    downstream = result("downstream")
    downstream.root_cause = downstream.root_cause.model_copy(
        update={"id": "downstream_root", "root_node": "gmv"}
    )
    downstream.trigger = trigger.root_cause.id
    first, second = (
        (trigger, downstream) if trigger_arrives_first else (downstream, trigger)
    )
    second.entry.ts += timedelta(hours=1)
    original = await store.upsert(first)
    merged = await store.upsert(second)
    assert merged.root_cause.id == trigger.root_cause.id
    assert set(merged.investigation_ids) == {"trigger", "downstream"}
    if trigger_arrives_first:
        assert merged.id == original.id
    else:
        assert store.get(original.id).merged_into == merged.id


@pytest.mark.asyncio
async def test_lifecycle_lookup_error_leaves_incident_unchanged(store):
    incident = await store.upsert(result())

    def unavailable(*args):
        raise LookupError("missing metric")

    with pytest.raises(LookupError, match="missing metric"):
        store.lifecycle(incident.entry.ts + timedelta(hours=1), unavailable)
    assert store.get(incident.id) == incident


@pytest.mark.asyncio
async def test_attached_anomalies_do_not_skip_lifecycle(store):
    incident = await store.upsert(result())
    for hour, z in ((1, 0), (2, 5), (3, 5)):
        ts = incident.entry.ts + timedelta(hours=hour)
        anomaly = incident.entry.model_copy(update={"ts": ts})
        attached = store.match(
            SentinelDecision(anomaly=anomaly, action="investigate", reason="Recurring")
        )
        assert attached.updated_ts == ts
        store.lifecycle(ts, lambda *_, z=z: z)
        current = store.get(incident.id)
        assert current.normal_ticks == (1 if hour == 1 else 0)
        assert current.impact_eur_total == hour * incident.impact_eur_per_hour
        if hour >= 2:
            assert current.status == "persisting"
        assert store.lifecycle(ts, lambda *_: 0) == []


@pytest.mark.asyncio
async def test_merge_preserves_absorbed_incidents_processed_tick(store):
    trigger = result("trigger")
    trigger.entry.metric = "upstream"
    trigger.root_cause = trigger.root_cause.model_copy(
        update={"id": "trigger_root", "root_node": "upstream"}
    )
    pending = store.match(
        SentinelDecision(
            anomaly=trigger.entry, action="investigate", reason="Trigger investigation"
        )
    )
    downstream = result("downstream", hours=1)
    downstream.trigger = trigger.root_cause.id
    await store.upsert(downstream)
    ts = trigger.entry.ts + timedelta(hours=2)
    store.lifecycle(ts, lambda *_: 0)
    survivor = await store.upsert(trigger)
    assert survivor.id == pending.id
    assert survivor.impact_eur_total == downstream.entry.impact_eur_per_hour
    assert store.lifecycle(ts, lambda *_: 0) == []
    assert store.get(survivor.id) == survivor


@pytest.mark.asyncio
async def test_causal_chain_links_without_merging(store):
    store.path_exists = lambda src, dst: (src, dst) == ("cart_to_checkout_rate", "gmv")
    first = await store.upsert(result())
    other = result("b", hours=1)
    other.root_cause = other.root_cause.model_copy(update={"root_node": "gmv"})
    second = await store.upsert(other)
    assert second.id != first.id
    assert first.id in second.related_incident_ids
    assert second.id in store.get(first.id).related_incident_ids


@pytest.mark.asyncio
async def test_lifecycle_and_reopen(store):
    incident = await store.upsert(result())
    ts = incident.entry.ts
    assert store.lifecycle(ts, lambda *_: 5) == []
    for hour in (1, 2):
        store.lifecycle(ts + timedelta(hours=hour), lambda *_: -5)
    assert store.get(incident.id).status == "persisting"
    store.lifecycle(ts + timedelta(hours=3), lambda *_: 0)
    store.lifecycle(ts + timedelta(hours=4), lambda *_: 4)
    assert store.get(incident.id).normal_ticks == 0
    for hour in (5, 6, 7):
        store.lifecycle(ts + timedelta(hours=hour), lambda *_: 0)
    resolved = store.get(incident.id)
    assert resolved.status == "resolved"
    assert resolved.resolved_ts == ts + timedelta(hours=7)
    assert resolved.impact_eur_total == 6 * incident.impact_eur_per_hour
    reopened = await store.upsert(result("b", hours=8))
    assert reopened.id == incident.id
    assert reopened.status == "open"
    assert reopened.resolved_ts is None
    assert reopened.occurrences == 2


@pytest.mark.asyncio
async def test_no_reopen_after_24_hours(store):
    incident = await store.upsert(result())
    for hour in (1, 2, 3):
        store.lifecycle(incident.entry.ts + timedelta(hours=hour), lambda *_: 0)
    assert (await store.upsert(result("b", hours=28))).id != incident.id


@pytest.mark.asyncio
async def test_deferred_and_innocent(store):
    value = result()
    value.verdict = "DATA_ISSUE"
    value.stop_reason = "data_issue"
    incident = await store.upsert(value)
    assert incident.status == "deferred"
    for hour, reading in ((1, (0, 0.8)), (2, (3, 1.0)), (3, 0)):
        store.lifecycle(
            value.entry.ts + timedelta(hours=hour), lambda *_, reading=reading: reading
        )
        assert store.get(incident.id).status == "deferred"
    store.lifecycle(value.entry.ts + timedelta(hours=4), lambda *_: (0, 0.9))
    assert store.get(incident.id).status == "resolved"
    value = result("innocent", hours=5)
    value.verdict = "INNOCENT"
    value.stop_reason = "innocent"
    assert (await store.upsert(value)).status == "innocent"


@pytest.mark.asyncio
async def test_notification_and_failure(store):
    calls = []

    async def notify(alert):
        calls.append(alert)
        return SellerAlertAck(received=True, actions_taken=["review"])

    store.notify = notify
    value = result()
    assert not (await store.upsert(value)).notifications
    value.affected_sellers = ["S-03"]
    incident = await store.upsert(value)
    assert calls[0].incident_id == incident.id
    assert incident.notifications[-1]["ack"]["received"]

    async def unavailable(alert):
        raise ConnectionError("offline")

    store.notify = unavailable
    assert not (await store.upsert(value)).notifications[-1]["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("other_hour", [-1, 1])
async def test_notification_preserves_updates_during_await(store, other_hour):
    concurrent = None

    async def notify(alert):
        nonlocal concurrent
        concurrent = await store.upsert(result("concurrent", hours=other_hour))
        return SellerAlertAck(received=True, actions_taken=["review"])

    store.notify = notify
    value = result()
    value.affected_sellers = ["S-03"]
    incident = await store.upsert(value)
    assert incident.id == concurrent.id
    assert set(incident.investigation_ids) == {"a", "concurrent"}
    assert incident.occurrences == 2
    assert len(incident.notifications) == 1
    assert incident.notifications[0]["ack"]["received"]
    assert store.get(incident.id) == incident


@pytest.mark.asyncio
async def test_persistence_and_trail(tmp_path):
    path = tmp_path / "ops.duckdb"
    store = IncidentStore(path)
    incident = await store.upsert(result())
    steps = make_trail_s7()
    for step in reversed(steps):
        store.add_step(step)
    store.close()
    store = IncidentStore(path)
    assert store.get(incident.id) == incident
    assert store.trail(steps[0].investigation_id) == steps
    assert store.trail("missing") == []
    assert store.get("missing") is None
    store.close()


@pytest.mark.asyncio
async def test_reporter_retry_then_fallback():
    calls = []

    async def wrong(**kwargs):
        calls.append(kwargs)
        return {"title": "Orders", "summary": "Loss EUR 987654321 per hour."}

    reporter = Reporter(wrong)
    assert await reporter.write(result()) == reporter.template(result())
    assert len(calls) == 2
    assert calls[0]["output_config"]["effort"] == "medium"
    assert "tools" not in calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, {}, {"title": "", "summary": ""}])
async def test_reporter_invalid_output(bad):
    async def invalid(**kwargs):
        return bad

    assert await Reporter(invalid).write(result()) == Reporter.template(result())


@pytest.mark.asyncio
async def test_reporter_valid_retry_and_exception():
    calls = 0

    async def llm(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unavailable")
        return {
            "title": "Orders below forecast",
            "summary": "Impact EUR 97,200 per hour; orders fell 40%.",
        }

    title, summary = await Reporter(llm).write(result())
    assert title == "Orders below forecast"
    assert "97,200" in summary
    assert calls == 2
