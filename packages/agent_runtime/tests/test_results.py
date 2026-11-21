import pytest
from agent_runtime import AgentResult
from pydantic import ValidationError


def test_done_requires_result() -> None:
    with pytest.raises(ValidationError, match="done requires a result"):
        AgentResult[str](status="done")


def test_needs_input_requires_question() -> None:
    with pytest.raises(ValidationError, match="needs_input requires a question"):
        AgentResult[str](status="needs_input")


@pytest.mark.parametrize("status", ["failed", "gave_up"])
def test_failure_states_require_reason(status: str) -> None:
    with pytest.raises(ValidationError, match="requires a reason"):
        AgentResult[str].model_validate({"status": status})


def test_free_text_status_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentResult[str].model_validate({"status": "stopped", "reason": "x"})


@pytest.mark.parametrize(
    ("status", "field", "a2a"),
    [
        ("done", {"result": "ok"}, "completed"),
        ("needs_input", {"question": "which SKU?"}, "input-required"),
        ("failed", {"reason": "boom"}, "failed"),
        ("gave_up", {"reason": "stuck"}, "failed"),
    ],
)
def test_maps_to_a2a_task_state(status: str, field: dict[str, str], a2a: str) -> None:
    assert AgentResult[str].model_validate({"status": status, **field}).a2a_state == a2a


def test_result_payload_is_validated() -> None:
    with pytest.raises(ValidationError):
        AgentResult[int](status="done", result="not a number")
