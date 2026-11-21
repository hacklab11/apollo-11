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
