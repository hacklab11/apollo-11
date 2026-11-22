"""Tenant-scoped wire contracts; v1 remains available in eye.contracts."""

import re
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent_runtime.budget import Budget
from agent_runtime.results import (
    A2A_TASK_STATES,
    AgentResult,
    EndState,
    PendingApproval,
    RunUsageSummary,
)
from eye.contracts import Anomaly, Contribution, Hypothesis, Symptom, Verdict

__all__ = [
    "A2A_TASK_STATES",
    "DEFAULT_TENANT",
    "INCIDENT_TRANSITIONS",
    "AgentResult",
    "Budget",
    "EndState",
    "IncidentCloseRequest",
    "IncidentClosure",
    "IncidentStatus",
    "IncidentV2",
    "PendingApproval",
    "RunUsageSummary",
    "TaskMetadata",
    "TenantId",
    "TenantScopedModel",
]

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
