"""Offline coverage of the scenario oracle checker."""

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from factories import CAMPAIGN_TS, make_incident_s7

from eye.contracts import Contribution, TrailStep

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
            "actual": "scenario S8 not available in the simulator",
        }
    ]
    assert result["llm_calls"] == 0


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


@pytest.mark.parametrize("name", ["I1", "I2"])
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
