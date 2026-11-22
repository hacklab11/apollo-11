import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from agent_runtime import budget, results
from eye.contracts_v2 import (
    A2A_TASK_STATES,
    DEFAULT_TENANT,
    INCIDENT_TRANSITIONS,
    AgentResult,
    Budget,
    EndState,
    IncidentCloseRequest,
    IncidentClosure,
    IncidentStatus,
    IncidentV2,
    PendingApproval,
    RunUsageSummary,
    TaskMetadata,
    TenantId,
    TenantScopedModel,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture
def incident_data(repo_root: Path) -> dict[str, Any]:
    data = json.loads((repo_root / "tests/fixtures/web/incident_s7.json").read_text())
    data.pop("resolved_ts")
    data.pop("normal_ticks")
    return {
        **data,
        "tenant_id": DEFAULT_TENANT,
        "context_id": "ctx_1",
        "status": "open",
    }


@pytest.fixture
def closure_data() -> dict[str, Any]:
    return {
        "tenant_id": DEFAULT_TENANT,
        "closed_by": "person_1",
        "note": "Recovery verified",
        "closed_ts": NOW,
    }


@pytest.fixture
def metadata_data() -> dict[str, Any]:
    return {
        "tenant_id": DEFAULT_TENANT,
        "context_id": "ctx_1",
        "traceparent": TRACEPARENT,
        "budget": {"tokens": 100, "cost_usd": 1.0, "deadline": NOW},
    }


def test_runtime_types_are_reexports() -> None:
    assert AgentResult is results.AgentResult
    assert Budget is budget.Budget
    assert EndState is results.EndState
    assert A2A_TASK_STATES is results.A2A_TASK_STATES
    assert PendingApproval is results.PendingApproval
    assert RunUsageSummary is results.RunUsageSummary


@pytest.mark.parametrize(
    ("status", "payload", "a2a_state"),
    [
        ("done", {"result": {}}, "completed"),
        ("needs_input", {"question": "Which tenant?"}, "input-required"),
        ("failed", {"reason": "Unavailable"}, "failed"),
        ("gave_up", {"reason": "No evidence"}, "failed"),
    ],
)
def test_end_states(status: str, payload: dict[str, Any], a2a_state: str) -> None:
    result = AgentResult.model_validate({"status": status, **payload})
    assert result.a2a_state == a2a_state
    assert AgentResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        AgentResult.model_validate({"status": status})


def test_unknown_end_state() -> None:
    with pytest.raises(ValidationError):
        AgentResult(status="resolved", result="ok")


def test_transition_table() -> None:
    assert set(INCIDENT_TRANSITIONS) == set(get_args(IncidentStatus))
    assert INCIDENT_TRANSITIONS == {
        "investigating": {"open"},
        "open": {"mitigating"},
        "mitigating": {"recovered"},
        "recovered": {"open", "mitigating", "closed"},
        "closed": set(),
    }


@pytest.mark.parametrize("status", get_args(IncidentStatus))
def test_closure_iff_closed(
    incident_data: dict[str, Any], closure_data: dict[str, Any], status: str
) -> None:
    incident_data["status"] = status
    if status == "closed":
        with pytest.raises(ValidationError, match="closure"):
            IncidentV2.model_validate(incident_data)
        incident_data["closure"] = closure_data
        assert IncidentV2.model_validate(incident_data).closure is not None
    else:
        assert IncidentV2.model_validate(incident_data).closure is None
        with pytest.raises(ValidationError, match="closure"):
            IncidentV2.model_validate({**incident_data, "closure": closure_data})


def test_closure_cannot_cross_tenants(
    incident_data: dict[str, Any], closure_data: dict[str, Any]
) -> None:
    closure_data["tenant_id"] = "other"
    with pytest.raises(ValidationError, match="must match"):
        IncidentV2.model_validate(
            {**incident_data, "status": "closed", "closure": closure_data}
        )


@pytest.mark.parametrize("value", ["", " ", "\n\t"])
def test_nonblank_closure_text(closure_data: dict[str, Any], value: str) -> None:
    for field in ("closed_by", "note"):
        with pytest.raises(ValidationError):
            IncidentClosure.model_validate({**closure_data, field: value})
    with pytest.raises(ValidationError):
        IncidentCloseRequest(tenant_id=DEFAULT_TENANT, note=value)


def test_closure_timestamp_requires_timezone(closure_data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IncidentClosure.model_validate(
            {**closure_data, "closed_ts": datetime(2026, 1, 1)}
        )


@pytest.mark.parametrize("tenant", ["default", "a", "0", "org-1_team", "a" * 63])
def test_tenant_ids(tenant: str) -> None:
    assert TypeAdapter(TenantId).validate_python(tenant) == tenant


@pytest.mark.parametrize("tenant", ["", "Upper", "-a", "_a", "a.b", "a/b", "a" * 64])
def test_invalid_tenant_ids(tenant: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(TenantId).validate_python(tenant)


def test_tenancy_required_and_extras_forbidden(
    incident_data: dict[str, Any],
    closure_data: dict[str, Any],
    metadata_data: dict[str, Any],
) -> None:
    examples = [
        (TenantScopedModel, {"tenant_id": DEFAULT_TENANT}),
        (IncidentV2, incident_data),
        (IncidentClosure, closure_data),
        (IncidentCloseRequest, {"tenant_id": DEFAULT_TENANT, "note": "Done"}),
        (TaskMetadata, metadata_data),
    ]
    for model, data in examples:
        value = model.model_validate(data)
        assert model.model_validate_json(value.model_dump_json()) == value
        with pytest.raises(ValidationError, match="tenant_id"):
            model.model_validate({k: v for k, v in data.items() if k != "tenant_id"})
        with pytest.raises(ValidationError, match="extra_forbidden"):
            model.model_validate({**data, "unexpected": True})


@pytest.mark.parametrize(
    "traceparent",
    [
        "",
        TRACEPARENT.upper(),
        TRACEPARENT[:-1],
        TRACEPARENT + "-extra",
        TRACEPARENT.replace("00-", "ff-", 1),
        "00-" + "0" * 32 + "-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01",
    ],
)
def test_invalid_traceparent(metadata_data: dict[str, Any], traceparent: str) -> None:
    with pytest.raises(ValidationError, match="traceparent"):
        TaskMetadata.model_validate({**metadata_data, "traceparent": traceparent})


def test_closed_incident_json_round_trip(
    incident_data: dict[str, Any], closure_data: dict[str, Any]
) -> None:
    incident = IncidentV2.model_validate(
        {**incident_data, "status": "closed", "closure": closure_data}
    )
    assert IncidentV2.model_validate_json(incident.model_dump_json()) == incident
