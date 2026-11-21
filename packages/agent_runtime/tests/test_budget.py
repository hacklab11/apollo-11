from datetime import UTC, datetime, timedelta

import pytest
from agent_runtime import Budget, DelegationRefused, RunLimits

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def budget(**overrides: object) -> Budget:
    fields: dict[str, object] = {
        "tokens": 10_000,
        "cost_usd": 1.0,
        "deadline": NOW + timedelta(seconds=60),
        "max_depth": 2,
        "max_fan_out": 3,
    }
    return Budget.model_validate(fields | overrides)


def test_spend_never_goes_negative() -> None:
    left = budget().spend(tokens=12_000, cost_usd=0.4)
    assert (left.tokens, left.cost_usd) == (0, 0.6)
    assert left.exhausted(NOW)


def test_delegation_splits_remaining_budget_and_deepens() -> None:
    child = budget().delegate(fan_out=2)
    assert (child.tokens, child.cost_usd, child.depth) == (5_000, 0.5, 1)
    assert child.deadline == budget().deadline


def test_delegation_depth_limit() -> None:
    grandchild = budget().delegate().delegate()
    with pytest.raises(DelegationRefused, match="depth 3 exceeds max 2"):
        grandchild.delegate()


def test_fan_out_limit() -> None:
    with pytest.raises(DelegationRefused, match="fan-out 4 exceeds max 3"):
        budget().delegate(fan_out=4)


def test_deadline_exhausts_budget() -> None:
    assert budget().seconds_left(NOW) == 60
    assert budget().exhausted(NOW + timedelta(seconds=61))


def test_metadata_round_trip_for_a2a() -> None:
    original = budget().delegate()
    assert Budget.from_metadata(original.to_metadata()) == original
    assert Budget.from_metadata({"other": 1}) is None


def test_run_limits_tighten_to_budget() -> None:
    remaining = budget(
        tokens=500, cost_usd=0.01, deadline=datetime.now(UTC) + timedelta(seconds=5)
    )
    limits = RunLimits(
        max_tokens=10_000, max_cost_usd=0.5, max_wall_clock_s=120
    ).within(remaining)
    assert limits.max_tokens == 500
    assert limits.max_cost_usd == 0.01
    assert limits.max_wall_clock_s <= 5
