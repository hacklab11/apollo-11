"""Offline coverage of the scenario oracle checker."""

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from factories import CAMPAIGN_TS, make_incident_s7
from pydantic import BaseModel

from eye.config import A2aSettings, LlmSettings
from eye.contracts import Contribution, TrailStep
from eye.llm.client import LLMUnavailable

SPEC = spec_from_file_location(
    "eval_scenarios", Path(__file__).resolve().parents[1] / "scripts/eval_scenarios.py"
)
assert SPEC and SPEC.loader
harness = module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def oracle_entry() -> dict:
    entry = deepcopy(harness.load_oracle()["S7"])
    entry["start_ts"] = CAMPAIGN_TS.isoformat()
    return entry


def test_oracle_ids() -> None:
    oracle = harness.load_oracle()
    assert set(oracle) == {
        *(f"S{i}" for i in range(1, 9)),
        "I1",
        "I2",
        "I3",
        *(f"D{i}" for i in range(1, 7)),
    }
    for entry in oracle.values():
        assert entry["play"] and entry["run_hours"] > 0 and entry["expect"]
        assert harness.evaluate(entry, [], [])
    assert next(iter(oracle)) == "D6"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["S8", "D6"])
async def test_unavailable_s8_fails_clearly(name: str) -> None:
    result = await harness.run_scenario(name, harness.load_oracle()[name], object())
    assert result["checks"] == [
        {
            "check": "simulator_available",
            "passed": False,
            "actual": "scenario S8 not available in the simulator; scenario I3 not available in the simulator",
        }
    ]
    assert result["llm_calls"] == 0


@pytest.mark.asyncio
async def test_unavailable_coupon_day_fails_clearly() -> None:
    result = await harness.run_scenario("I3", harness.load_oracle()["I3"], object())
    assert result["checks"][0]["actual"] == "scenario I3 not available in the simulator"


def test_story_oracles() -> None:
    for name in ("S8", "D6"):
        entry = harness.load_oracle()[name]
        assert entry["play"] == ["I3@0", "S8@10"]
        assert "Black Friday" not in entry["expect"]["must_mention"]
        assert "stockout" in entry["expect"]["must_not_root_cause"]
        assert entry["expect"]["metric_attached"] == {
            "metric": "fee_per_order",
            "segment": {"category": "electronics", "seller_tier": "3P"},
        }
    assert (
        harness.load_oracle()["D6"]["on_incident_open"] == "add_metric(fee_per_order)"
    )


def test_matching_uses_report_and_window_not_just_entry() -> None:
    entry = deepcopy(harness.load_oracle()["S3"])
    entry["start_ts"] = CAMPAIGN_TS.isoformat()
    good = make_incident_s7()
    root = good.root_cause.model_copy(
        update={"root_node": "first_party_margin", "segment": {"country": "DE"}}
    )
    incident = good.model_copy(
        update={
            "opened_ts": CAMPAIGN_TS,
            "entry": good.entry.model_copy(update={"metric": "first_party_margin"}),
            "summary": "DE search-ranker v7.2 reduced relevance",
            "root_cause": root,
            "hypotheses": [root],
        }
    )
    unrelated = good.model_copy(
        update={
            "id": "unrelated",
            "title": "Budget spent",
            "summary": "Budget spent",
            "suggested_actions": [],
            "open_questions": [],
            "investigation_ids": ["budget-investigation"],
            "entry": good.entry.model_copy(update={"metric": "ad_spend"}),
            "root_cause": root.model_copy(update={"cause_type": "pacing_bug"}),
            "hypotheses": [],
        }
    )
    outside = incident.model_copy(
        update={"id": "outside", "opened_ts": CAMPAIGN_TS - harness.HOUR}
    )
    results = {
        c["check"]: c
        for c in harness.evaluate(entry, [incident, unrelated, outside], [])
    }
    assert results["expect.entry_metrics_any"]["actual"] == ["first_party_margin"]
    assert not results["expect.entry_metrics_any"]["passed"]
    assert results["expect.verdict_any"]["actual"] == ["CONFIRMED"]
    assert results["expect.root_node_any"]["actual"] == ["first_party_margin"]
    assert results["expect.max_incidents_for_scenario"]["actual"] == 1


def test_matching_chooses_best_candidate_and_counts_matches() -> None:
    good = make_incident_s7()
    weaker = good.model_copy(update={"id": "weaker", "verdict": "HYPOTHESIS"})
    results = {
        c["check"]: c for c in harness.evaluate(oracle_entry(), [weaker, good], [])
    }
    assert results["expect.verdict_any"]["actual"] == ["CONFIRMED"]
    assert results["expect.max_incidents_for_scenario"]["actual"] == 2


def test_matching_cause_in_report_and_coupon_window() -> None:
    good = make_incident_s7()
    incident = good.model_copy(
        update={
            "summary": "Unplanned price_change caused this",
            "root_cause": None,
            "hypotheses": [],
            "opened_ts": CAMPAIGN_TS + 11 * harness.HOUR,
        }
    )
    entry = {
        "play": ["I3@0", "S8@10"],
        "run_hours": 20,
        "start_ts": CAMPAIGN_TS.isoformat(),
        "expect": {
            "cause_type": "price_change",
            "entry_metrics_any": ["aov"],
            "max_incidents_for_scenario": 1,
        },
    }
    before = incident.model_copy(
        update={"id": "before", "opened_ts": CAMPAIGN_TS + harness.HOUR}
    )
    results = {c["check"]: c for c in harness.evaluate(entry, [before, incident], [])}
    assert results["expect.entry_metrics_any"]["actual"] == [incident.entry.metric]
    assert results["expect.max_incidents_for_scenario"]["actual"] == 1


@pytest.mark.asyncio
async def test_replay_miss_never_calls_sdk(tmp_path: Path) -> None:
    client = harness.EvalLLMClient(
        LlmSettings(mode="replay", cache_path=tmp_path / "absent.jsonl"),
        tmp_path / "requests.jsonl",
    )
    with pytest.raises(LLMUnavailable, match="Offline replay"):
        await client.create(
            {"model": "test", "system": [], "tools": [], "messages": []}
        )
    assert client._sdk is None
    assert client.replay_misses == 1


@pytest.mark.asyncio
async def test_live_response_mapping_order_matches_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Response(BaseModel):
        content: dict

    async def create(self: object, request: dict) -> Response:
        return Response(content={"segment": {"country": "DE", "channel": "search"}})

    monkeypatch.setattr(harness.LLMClient, "create", create)
    client = harness.EvalLLMClient(LlmSettings(), tmp_path / "requests.jsonl")
    response = await client.create(
        {"model": "test", "system": [], "tools": [], "messages": []}
    )
    assert list(response.content["segment"]) == ["channel", "country"]


@pytest.mark.asyncio
async def test_a2a_snapshot_replays_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cards(self: object) -> dict:
        return {
            "discovery": harness.AgentCardInfo(
                name="discovery", url="http://localhost:8200/discovery", healthy=False
            )
        }

    monkeypatch.setattr(harness.A2AClient, "fetch_cards", cards)
    fixture = tmp_path / "fixture.jsonl"
    recorded = harness.EvalA2AClient(A2aSettings(), "record", fixture)
    expected = await recorded.fetch_cards()
    await recorded.aclose()

    async def forbidden(self: object) -> dict:
        raise AssertionError("replay reached network boundary")

    monkeypatch.setattr(harness.A2AClient, "fetch_cards", forbidden)
    replayed = harness.EvalA2AClient(A2aSettings(), "replay", fixture)
    try:
        assert await replayed.fetch_cards() == expected
        assert replayed.replay_misses == 0
    finally:
        await replayed.aclose()


def test_seeded_identity_streams() -> None:
    def values(seed: int) -> list:
        stream = harness.seeded_ids(seed, "S3", "detective")
        return [next(stream) for _ in range(3)]

    assert values(42) == values(42)
    assert len(set(values(42))) == 3
    assert values(42) != values(43)


@pytest.mark.parametrize(
    "segment,passed",
    [
        ({"category": "electronics", "seller_tier": "3P"}, True),
        ({"category": "electronics"}, False),
        ({"category": "electronics", "seller_tier": "1P"}, False),
    ],
)
def test_attached_metric_requires_3p_electronics(segment: dict, passed: bool) -> None:
    incident = make_incident_s7()
    step = TrailStep(
        id="attached-metric",
        investigation_id=incident.investigation_ids[0],
        n=1,
        real_ts=CAMPAIGN_TS,
        actor="sentinel",
        action="decision",
        output_summary="Attached fee_per_order to the existing incident",
        output={
            "action": "attach",
            "attach_to_incident": incident.id,
            "anomaly": {"metric": "fee_per_order", "segment": segment},
        },
    )
    entry = {
        "expect": {
            "metric_attached": harness.load_oracle()["D6"]["expect"]["metric_attached"]
        }
    }
    assert harness.evaluate(entry, [incident], [step])[0]["passed"] == passed
    assert not harness.evaluate(entry, [incident], [])[0]["passed"]


def test_refuted_hypothesis_does_not_supply_root_or_segment() -> None:
    incident = make_incident_s7()
    entry = {
        "expect": {
            "root_nodes_all": ["payment_success_rate"],
            "segment_contains": {"psp": ["PSP-2"]},
        }
    }
    results = harness.evaluate(entry, [incident], [])
    assert not any(c["passed"] for c in results)


def test_contributions_and_trail_checks() -> None:
    incident = make_incident_s7().model_copy(
        update={
            "contributions": [
                Contribution(
                    factor="marketing_cost", kind="planned", delta=-55, share=0.55
                )
            ]
        }
    )
    entry = {
        "expect": {
            "root_nodes_all": ["marketing_cost"],
            "contribution_kind": "planned",
            "contribution_shares": {"marketing_cost": [0.45, 0.65]},
            "affected_sellers": ["S-03"],
            "notified_sellers": ["S-03"],
            "loop_contains": ["buybox_share"],
        }
    }
    step = TrailStep(
        id="eval",
        investigation_id="inv_s7",
        n=1,
        real_ts=CAMPAIGN_TS,
        actor="orchestrator",
        action="decision",
        output_summary="1 notified seller",
        output={
            "affected_sellers": ["S-03"],
            "seller_ids": ["S-03"],
            "received": True,
            "loop": ["buybox_share"],
        },
    )
    assert all(c["passed"] for c in harness.evaluate(entry, [incident], [step]))
    assert not all(c["passed"] for c in harness.evaluate(entry, [incident], []))


def test_correct_report_passes() -> None:
    results = harness.evaluate(oracle_entry(), [make_incident_s7()], [])
    assert all(c["passed"] for c in results), results
    assert (
        next(c["actual"] for c in results if c["check"] == "max_ticks_to_detect") == 1
    )


@pytest.mark.parametrize(
    "field,value,check",
    [
        ("verdict", "HYPOTHESIS", "verdict_any"),
        ("summary", "No useful report", "must_mention"),
        ("suggested_actions", [], "suggested_actions_mention"),
        ("status", "resolved", "status_any"),
    ],
)
def test_failed_checks_include_actual(field: str, value: object, check: str) -> None:
    entry = oracle_entry()
    entry["expect"]["status_any"] = ["open"]
    incident = make_incident_s7().model_copy(update={field: value})
    results = harness.evaluate(entry, [incident], [])
    result = next(c for c in results if c["check"] == f"expect.{check}")
    assert not result["passed"]
    assert "actual" in result


def test_empty_business_scenario_fails() -> None:
    results = harness.evaluate(oracle_entry(), [], [])
    assert not any(c["passed"] for c in results if not c["check"].startswith("also."))


@pytest.mark.parametrize("name", ["I1", "I2", "I3"])
def test_innocent_and_deferred_may_have_no_incident(name: str) -> None:
    assert all(
        c["passed"] for c in harness.evaluate(harness.load_oracle()[name], [], [])
    )


def test_duplicate_incident_fails() -> None:
    incident = make_incident_s7()
    duplicate = incident.model_copy(update={"id": "duplicate"})
    results = harness.evaluate(oracle_entry(), [incident, duplicate], [])
    count = next(
        c for c in results if c["check"] == "expect.max_incidents_for_scenario"
    )
    assert count == {
        "check": "expect.max_incidents_for_scenario",
        "passed": False,
        "actual": 2,
    }


def test_unknown_check_errors() -> None:
    with pytest.raises(ValueError, match="Unknown oracle check"):
        harness.evaluate({"expect": {"typo": True}}, [], [])


def test_forbidden_root_and_insufficient_evidence_fail() -> None:
    incident = make_incident_s7()
    root = incident.root_cause.model_copy(
        update={"cause_type": "traffic_change", "evidence_for": []}
    )
    incident = incident.model_copy(update={"root_cause": root})
    results = harness.evaluate(oracle_entry(), [incident], [])
    assert not next(
        c["passed"] for c in results if c["check"] == "expect.must_not_root_cause"
    )
    assert not next(
        c["passed"] for c in results if c["check"] == "expect.evidence_groups_min"
    )


def test_reports_cannot_supply_each_others_checks() -> None:
    good = make_incident_s7()
    first = good.model_copy(update={"verdict": "HYPOTHESIS"})
    second = good.model_copy(update={"summary": "Nothing relevant", "id": "other"})
    assert not all(
        c["passed"] for c in harness.evaluate(oracle_entry(), [first, second], [])
    )
