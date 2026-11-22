"""Shared wire contracts for data, agents, services and tenant-scoped execution."""

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

Dimension = Literal[
    "country", "platform", "category", "seller_tier", "channel", "advertiser_id", "psp"
]

Country = Literal["DE", "FR"]
Platform = Literal["android", "ios", "web"]
Category = Literal["electronics", "home", "fashion", "toys"]
SellerTier = Literal["1P", "large_3P", "small_3P"]
Channel = Literal["search", "feed", "other"]

MetricKind = Literal["sql", "formula"]
EdgeType = Literal["formula", "breakdown", "causal_hypothesis"]
EdgeStatus = Literal["validated", "ambiguous", "rejected", "hypothesis"]
AnomalyKind = Literal["point", "divergent_segment", "share_shift"]
SentinelAction = Literal["investigate", "watch", "defer", "ignore", "attach"]

CauseType = Literal[
    "release",
    "model_change",
    "config_change",
    "pacing_bug",
    "traffic_change",
    "planned_event",
    "price_change",
    "repricer_loop",
    "cpc_change",
    "stockout",
    "supplier_delay",
    "po_shortfall",
    "capacity_limit",
    "hijacker",
    "discount",
    "mix_shift",
    "fee_change",
    "marketing_spend",
    "data_gap",
    "seasonality",
    "unknown",
]
HypothesisStatus = Literal["open", "supported", "refuted"]
EvidenceGroup = Literal[
    "data", "change_log", "engineering", "specialist", "external", "freshness"
]
Verdict = Literal["CONFIRMED", "HYPOTHESIS", "INNOCENT", "MIXED", "DATA_ISSUE"]
PublicIncidentStatus = Literal[
    "investigating", "open", "persisting", "resolved", "deferred", "innocent"
]
Actor = Literal[
    "orchestrator",
    "sentinel",
    "detective",
    "skeptic",
    "reporter",
    "cartographer",
    "growth",
    "marketplace",
    "discovery",
    "selleros",
    "chat",
]
StepAction = Literal["llm_call", "tool_call", "a2a_call", "decision", "note"]


class MetricDef(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")
    description: str
    unit: Literal["count", "eur", "ratio", "hours", "ms", "index"]
    domain: Literal[
        "traffic",
        "search",
        "feed",
        "orders",
        "marketplace",
        "supply",
        "ads",
        "finance",
        "quality",
    ]
    kind: MetricKind
    sql: str | None = None
    expression: str | None = None
    dimensions: list[Dimension]
    higher_is_better: bool | None = None
    impact_weight: float = 0.0
    added_by: Literal["seed", "user"] = "seed"
    status: Literal["active", "pending"] = "active"


class Edge(BaseModel):
    id: str
    src: str
    dst: str
    type: EdgeType
    status: EdgeStatus
    identity: str | None = None
    dimension: Dimension | None = None
    lag_hours: int | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: dict
    created_by: Literal["bootstrap", "cartographer", "seed"]


class GraphView(BaseModel):
    nodes: list[dict]
    edges: list[Edge]


class SeriesPoint(BaseModel):
    ts: datetime
    value: float | None
    expected: float | None = None
    lower: float | None = None
    upper: float | None = None
    completeness: float = 1.0


class Series(BaseModel):
    metric: str
    segment: dict[str, str]
    points: list[SeriesPoint]


class Anomaly(BaseModel):
    id: str
    ts: datetime
    metric: str
    segment: dict[str, str]
    kind: AnomalyKind
    actual: float
    expected: float
    z: float
    delta: float
    delta_pct: float
    window_hours: int
    total_z: float | None = None
    share_now: float | None = None
    share_expected: float | None = None
    impact_eur_per_hour: float
    completeness: float
    priority: float


class SentinelDecision(BaseModel):
    anomaly: Anomaly
    action: SentinelAction
    reason: str
    attach_to_incident: str | None = None


class Evidence(BaseModel):
    group: EvidenceGroup
    source: str
    summary: str
    numbers: dict[str, float | str] = Field(default_factory=dict)
    step_id: str


class Hypothesis(BaseModel):
    id: str
    statement: str
    cause_type: CauseType
    root_node: str
    segment: dict[str, str]
    first_change_ts: datetime | None
    explained_share: float = Field(ge=0, le=1)
    status: HypothesisStatus
    evidence_for: list[Evidence] = Field(default_factory=list)
    evidence_against: list[Evidence] = Field(default_factory=list)


class Contribution(BaseModel):
    factor: str
    kind: Literal["formula", "mix", "rate", "segment", "planned", "residual"]
    delta: float
    share: float


class TrailStep(BaseModel):
    id: str
    investigation_id: str
    n: int
    real_ts: datetime
    actor: Actor
    action: StepAction
    tool: str | None = None
    input: dict = Field(default_factory=dict)
    output_summary: str = Field(max_length=500)
    output: dict | None = None
    reason: str = ""
    duration_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class Symptom(BaseModel):
    node: str
    first_change_ts: datetime


class InvestigationResult(BaseModel):
    investigation_id: str
    entry: Anomaly
    verdict: Verdict
    summary: str
    root_cause: Hypothesis | None
    hypotheses: list[Hypothesis]
    symptoms: list[Symptom] = Field(default_factory=list)
    contributions: list[Contribution]
    loop: list[str] | None = None
    trigger: str | None = None
    affected_actors: list[str] = Field(default_factory=list)
    affected_sellers: list[str] = Field(default_factory=list)
    open_questions: list[str]
    suggested_actions: list[str]
    steps_used: int
    stop_reason: Literal["confirmed", "budget", "innocent", "data_issue", "error"]


class Incident(BaseModel):
    id: str
    fingerprint: str
    title: str = Field(max_length=89)
    status: PublicIncidentStatus
    verdict: Verdict | None
    opened_ts: datetime
    updated_ts: datetime
    resolved_ts: datetime | None = None
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
    normal_ticks: int = 0


class Event(BaseModel):
    type: Literal[
        "tick",
        "anomaly",
        "sentinel_decision",
        "investigation_started",
        "trail_step",
        "investigation_finished",
        "incident_upserted",
        "graph_changed",
        "scenario_changed",
        "agent_status",
    ]
    sim_ts: datetime
    payload: dict


class SpecialistRequest(BaseModel):
    investigation_id: str
    question: str
    metric: str
    segment: dict[str, str]
    window_start_ts: datetime
    window_end_ts: datetime
    baseline_start_ts: datetime
    baseline_end_ts: datetime
    context: str = Field(max_length=1500)


class Finding(BaseModel):
    claim: str
    numbers: dict[str, float | str]
    confidence: float = Field(ge=0, le=1)
    supports: list[CauseType] = Field(default_factory=list)
    refutes: list[CauseType] = Field(default_factory=list)


class SpecialistResponse(BaseModel):
    agent: str
    answered: bool
    findings: list[Finding]
    suggested_next: list[str] = Field(default_factory=list)
    data_used: list[str] = Field(default_factory=list)


class SellerAlert(BaseModel):
    incident_id: str
    seller_ids: list[str]
    skus: list[str]
    summary: str
    recommended_action: str


class SellerAlertAck(BaseModel):
    received: bool
    actions_taken: list[str]


class AddMetricRequest(BaseModel):
    name: str
    description: str
    sql: str | None = None
    expression: str | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def check_source(self) -> Self:
        if (self.sql is None) == (self.expression is None):
            raise ValueError("exactly one of sql or expression is required")
        return self


class EdgeProposal(BaseModel):
    type: EdgeType
    src: str
    dst: str
    identity: str | None = None
    dimension: Dimension | None = None
    lag_hours: int | None = None
    rationale: str


class AddMetricResult(BaseModel):
    metric: MetricDef
    accepted: list[Edge]
    ambiguous: list[Edge]
    rejected: list[Edge]
    notes: str
    trail_investigation_id: str


ScenarioId = Literal["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "I1", "I2", "I3"]


class ScenarioState(BaseModel):
    id: ScenarioId
    title: str
    active: bool
    injected_ts: datetime | None
    cleared_ts: datetime | None


__all__ = [
    "A2A_TASK_STATES",
    "BUDGET_METADATA_KEY",
    "DEFAULT_TENANT",
    "INCIDENT_TRANSITIONS",
    "Actor",
    "AddMetricRequest",
    "AddMetricResult",
    "AgentResult",
    "Anomaly",
    "AnomalyKind",
    "Budget",
    "Category",
    "CauseType",
    "ChallengeRequest",
    "ChallengeResult",
    "Channel",
    "Contribution",
    "Country",
    "DetectRequest",
    "DetectResult",
    "Dimension",
    "Edge",
    "EdgeProposal",
    "EdgeStatus",
    "EdgeType",
    "EndState",
    "Event",
    "Evidence",
    "EvidenceGroup",
    "Finding",
    "GaveUp",
    "GraphView",
    "Hypothesis",
    "HypothesisStatus",
    "Incident",
    "IncidentCloseRequest",
    "IncidentClosure",
    "IncidentStatus",
    "IncidentV2",
    "InvestigateRequest",
    "InvestigateResult",
    "InvestigationResult",
    "MapRequest",
    "MetricDef",
    "MetricKind",
    "NeedsInput",
    "PendingApproval",
    "Platform",
    "PublicIncidentStatus",
    "ReportRequest",
    "ReportResult",
    "RunUsageSummary",
    "ScenarioId",
    "ScenarioState",
    "SellerAlert",
    "SellerAlertAck",
    "SellerPriceRevert",
    "SellerRevertApproval",
    "SellerTier",
    "SentinelAction",
    "SentinelDecision",
    "Series",
    "SeriesPoint",
    "SkepticResult",
    "SpecialistRequest",
    "SpecialistResponse",
    "StepAction",
    "Symptom",
    "TaskMetadata",
    "TenantId",
    "TenantScopedModel",
    "TrailStep",
    "Verdict",
]

EndState = Literal["done", "needs_input", "failed", "gave_up"]

SELLER_REVERT_TOOL = "revert_manual_price"
SELLER_REVERT_APPROVAL_METADATA_KEY = "seller_revert_approval_id"


class SellerPriceRevert(BaseModel):
    """The exact price change a human can authorize SellerOS to revert."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: str = Field(min_length=1, max_length=256)
    seller_id: str = Field(pattern=r"^S-[0-9]+$", max_length=32)
    sku: str = Field(min_length=1, max_length=64)
    changed_at: AwareDatetime
    price: float = Field(gt=0, allow_inf_nan=False)
    restore_price: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("changed_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    def idempotency_key(self) -> str:
        return uuid5(
            NAMESPACE_URL, f"SellerOS:{SELLER_REVERT_TOOL}:{self.model_dump_json()}"
        ).hex


class SellerRevertApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tool_name: Literal["revert_manual_price"]
    args: SellerPriceRevert
    status: Literal["pending", "approved", "rejected"]
    decided_by: str | None


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
