"""Explicit end states for every agent run."""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

EndState = Literal["done", "needs_input", "failed", "gave_up"]

A2A_TASK_STATES: dict[EndState, str] = {
    "done": "completed",
    "needs_input": "input-required",
    "failed": "failed",
    "gave_up": "failed",
}


class NeedsInput(BaseModel):
    """The agent cannot continue without an answer from a person or the caller."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)


class GaveUp(BaseModel):
    """The agent decided it cannot finish the task."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)


class PendingApproval(BaseModel):
    """A tool call held back until a person approves it."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    tool_name: str
    args: dict[str, Any]


class RunUsageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class AgentResult[T](BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: EndState
    result: T | None = None
    question: str | None = None
    reason: str | None = None
    pending_approvals: list[PendingApproval] = Field(default_factory=list)
    usage: RunUsageSummary = Field(default_factory=RunUsageSummary)

    @model_validator(mode="after")
    def _check_state(self) -> Self:
        if self.status == "done" and self.result is None:
            raise ValueError("done requires a result")
        if self.status == "needs_input" and not self.question:
            raise ValueError("needs_input requires a question")
        if self.status in ("failed", "gave_up") and not self.reason:
            raise ValueError(f"{self.status} requires a reason")
        return self

    @property
    def a2a_state(self) -> str:
        return A2A_TASK_STATES[self.status]
