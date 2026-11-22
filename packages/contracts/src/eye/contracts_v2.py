"""Tenant-scoped wire contracts; v1 remains available in eye.contracts."""

import re
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from eye.contracts import (
    AddMetricRequest,
    Anomaly,
    Contribution,
    Evidence,
    Hypothesis,
    InvestigationResult,
    SentinelDecision,
    Symptom,
    TrailStep,
    Verdict,
)

__all__ = [
    "A2A_TASK_STATES",
    "BUDGET_METADATA_KEY",
    "DEFAULT_TENANT",
    "INCIDENT_TRANSITIONS",
    "AgentResult",
    "Budget",
    "ChallengeRequest",
    "ChallengeResult",
    "DetectRequest",
    "DetectResult",
    "EndState",
    "GaveUp",
    "IncidentCloseRequest",
    "IncidentClosure",
    "IncidentStatus",
    "IncidentV2",
    "InvestigateRequest",
    "InvestigateResult",
    "MapRequest",
    "NeedsInput",
    "PendingApproval",
    "ReportRequest",
    "ReportResult",
    "RunUsageSummary",
    "SkepticResult",
    "TaskMetadata",
    "TenantId",
    "TenantScopedModel",
]

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


BUDGET_METADATA_KEY = "budget"


class Budget(BaseModel):
    """What is left for a run and everything it delegates to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    deadline: AwareDatetime
    depth: int = Field(default=0, ge=0)
    max_depth: int = Field(default=2, ge=0)
    max_fan_out: int = Field(default=3, ge=1)


TenantId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")]
DEFAULT_TENANT = "default"
IncidentStatus = Literal["investigating", "open", "mitigating", "recovered", "closed"]
INCIDENT_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    "investigating": frozenset({"open"}),
    "open": frozenset({"mitigating"}),
    "mitigating": frozenset({"recovered"}),
    "recovered": frozenset({"closed", "open", "mitigating"}),
    "closed": frozenset(),
}
NonEmptyText = Annotated[str, Field(min_length=1, pattern=r"\S")]
Traceparent = Annotated[
    str,
    Field(
        pattern=re.compile(
            r"^00-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}\Z"
        )
    ),
]


class TenantScopedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: TenantId


class IncidentClosure(TenantScopedModel):
    closed_by: NonEmptyText
    note: NonEmptyText
    closed_ts: AwareDatetime


class IncidentCloseRequest(TenantScopedModel):
    note: NonEmptyText


class IncidentV2(TenantScopedModel):
    id: str
    context_id: str
    fingerprint: str
    title: str = Field(max_length=89)
    status: IncidentStatus
    closure: IncidentClosure | None = None
    verdict: Verdict | None
    opened_ts: datetime
    updated_ts: datetime
    recovered_ts: datetime | None = None
    last_seen_ts: datetime
    occurrences: int = 1
    entry: Anomaly
    summary: str
    impact_eur_per_hour: float
    impact_eur_total: float
    affected_actors: list[str]
    contributions: list[Contribution]
    root_cause: Hypothesis | None
    hypotheses: list[Hypothesis]
    symptoms: list[Symptom] = Field(default_factory=list)
    investigation_ids: list[str]
    open_questions: list[str]
    suggested_actions: list[str]
    related_incident_ids: list[str] = Field(default_factory=list)
    merged_into: str | None = None
    notifications: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_closure(self) -> Self:
        if (self.status == "closed") != (self.closure is not None):
            raise ValueError("closure is required if and only if status is closed")
        if self.closure is not None and self.closure.tenant_id != self.tenant_id:
            raise ValueError("closure tenant_id must match incident tenant_id")
        return self


class TaskMetadata(TenantScopedModel):
    context_id: str
    traceparent: Traceparent
    budget: Budget


class _SkillPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DetectRequest(_SkillPayload):
    ts: AwareDatetime
    open_incidents: list[IncidentV2] = Field(default_factory=list)
    previous_decisions: list[SentinelDecision] = Field(default_factory=list)


class DetectResult(_SkillPayload):
    decisions: list[SentinelDecision]
    trail: list[TrailStep] = Field(default_factory=list)


class InvestigateRequest(_SkillPayload):
    anomaly: Anomaly
    investigation_id: str | None = None
    open_incidents: list[IncidentV2] = Field(default_factory=list)


class InvestigateResult(_SkillPayload):
    investigation: InvestigationResult
    trail: list[TrailStep] = Field(default_factory=list)


class ChallengeRequest(_SkillPayload):
    anomaly: Anomaly
    investigation_id: str
    hypothesis: Hypothesis | None = None


class SkepticResult(_SkillPayload):
    verdict: Literal["innocent", "partly_innocent", "not_innocent", "data_issue"]
    explained_share: float = Field(ge=0, le=1)
    reasons: list[str]
    evidence: list[Evidence]


class ChallengeResult(_SkillPayload):
    review: SkepticResult
    trail: list[TrailStep] = Field(default_factory=list)


class ReportRequest(_SkillPayload):
    investigation: InvestigationResult


class ReportResult(_SkillPayload):
    title: str = Field(min_length=1, max_length=89)
    summary: str = Field(min_length=1)


class MapRequest(_SkillPayload):
    metric: AddMetricRequest
