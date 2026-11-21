"""Shared contracts for the simulator, agents, and service boundaries."""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

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
IncidentStatus = Literal[
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
    status: IncidentStatus
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
