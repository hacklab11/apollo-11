# 02 Contracts

All shared data shapes. Implement them in `src/eye/contracts.py` with pydantic v2.
Every component, mock file, API, MCP tool, and A2A message uses these models.
Change a contract only in task T00 or with agreement from both developers. Then update this file first.

Conventions:
- Time: sim time, UTC, ISO 8601 string in JSON, `datetime` in Python. Field names ending in `_ts` are sim time. `real_ts` is wall-clock time.
- Money: EUR, float, 2 decimals in JSON.
- Shares: float 0..1. Percent only in display text.
- IDs: string prefixes `inc_`, `anm_`, `hyp_`, `stp_`, `edg_`, `inv_`.
- Segment: `dict[str, str]` of dimension → value, for example `{"country": "DE", "platform": "android"}`. Empty dict = total.

## 1. Enums

```python
Dimension = Literal["country", "platform", "category", "seller_tier", "channel", "advertiser_id", "psp"]
# advertiser_id: ads metrics only. psp: payment metrics only.
Country = Literal["DE", "FR"]
Platform = Literal["android", "ios", "web"]
Category = Literal["electronics", "home", "fashion", "toys"]
SellerTier = Literal["1P", "large_3P", "small_3P"]
Channel = Literal["search", "feed", "other"]

MetricKind = Literal["sql", "formula"]          # sql: SQL source; formula: expression over metrics
EdgeType = Literal["formula", "breakdown", "causal_hypothesis"]
EdgeStatus = Literal["validated", "ambiguous", "rejected", "hypothesis"]
AnomalyKind = Literal["point", "divergent_segment", "share_shift"]
SentinelAction = Literal["investigate", "watch", "defer", "ignore", "attach"]

CauseType = Literal[
  "release", "model_change", "config_change", "pacing_bug", "traffic_change",
  "planned_event", "price_change", "repricer_loop", "cpc_change", "stockout",
  "supplier_delay", "po_shortfall", "capacity_limit", "hijacker", "discount",
  "mix_shift", "fee_change", "marketing_spend", "data_gap", "seasonality", "unknown",
]
HypothesisStatus = Literal["open", "supported", "refuted"]
EvidenceGroup = Literal["data", "change_log", "engineering", "specialist", "external", "freshness"]
Verdict = Literal["CONFIRMED", "HYPOTHESIS", "INNOCENT", "MIXED", "DATA_ISSUE"]
IncidentStatus = Literal["investigating", "open", "persisting", "resolved", "deferred", "innocent"]
Actor = Literal["orchestrator", "sentinel", "detective", "skeptic", "reporter", "cartographer",
                "growth", "marketplace", "discovery", "selleros", "chat"]  # discovery = Discovery & Experience
StepAction = Literal["llm_call", "tool_call", "a2a_call", "decision", "note"]
```

## 2. Metrics and graph

```python
class MetricDef(BaseModel):
    name: str                      # ^[a-z][a-z0-9_]{2,40}$
    description: str               # plain English, 1–3 sentences
    unit: Literal["count", "eur", "ratio", "hours", "ms", "index"]
    domain: Literal["traffic", "search", "feed", "orders", "marketplace", "supply", "ads", "finance", "quality"]
    kind: MetricKind
    sql: str | None = None         # kind=sql: returns columns ts, <dims...>, num, den (den optional)
    expression: str | None = None  # kind=formula: e.g. "orders / sessions"
    dimensions: list[Dimension]    # dims the SQL returns; formula: intersection of inputs
    higher_is_better: bool | None = None
    impact_weight: float = 0.0     # filled by graph bootstrap (09)
    added_by: Literal["seed", "user"] = "seed"
    status: Literal["active", "pending"] = "active"

class Edge(BaseModel):
    id: str
    src: str                       # upstream: component, driver, or cause
    dst: str                       # downstream: outcome
    type: EdgeType
    status: EdgeStatus
    identity: str | None = None    # formula: the identity text, e.g. "orders = sessions * conversion_rate"
    dimension: Dimension | None = None   # breakdown only
    lag_hours: int | None = None   # causal only
    confidence: float              # 0..1
    evidence: dict                 # residual_pct, corr, best_lag, n_points, rationale
    created_by: Literal["bootstrap", "cartographer", "seed"]

class GraphView(BaseModel):       # API/MCP output
    nodes: list[dict]              # {id, label, domain, unit, state: normal|watch|anomaly|incident, impact_weight}
    edges: list[Edge]
```

Edge direction rule: `src` is upstream, `dst` is downstream. To investigate an anomaly at node X, walk the in-edges of X (from outcome toward causes).
Breakdown edges: `src == dst == metric`, with `dimension` set. The UI shows them as dimension chips on the node, not as lines.

## 3. Time series

```python
class SeriesPoint(BaseModel):
    ts: datetime
    value: float | None            # None = no data
    expected: float | None = None
    lower: float | None = None
    upper: float | None = None
    completeness: float = 1.0

class Series(BaseModel):
    metric: str
    segment: dict[str, str]
    points: list[SeriesPoint]
```

## 4. Anomalies and Sentinel decisions

```python
class Anomaly(BaseModel):
    id: str
    ts: datetime                   # sim hour of detection
    metric: str
    segment: dict[str, str]
    kind: AnomalyKind
    actual: float
    expected: float
    z: float
    delta: float                   # actual - expected
    delta_pct: float
    window_hours: int              # 1 for point; 6 for divergence scans
    total_z: float | None = None   # z of the same metric at total level
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
```

## 5. Investigation

```python
class Evidence(BaseModel):
    group: EvidenceGroup
    source: str                    # tool name or agent name
    summary: str                   # one sentence, contains the key number
    numbers: dict[str, float | str] = {}
    step_id: str                   # TrailStep that produced it

class Hypothesis(BaseModel):
    id: str
    statement: str
    cause_type: CauseType
    root_node: str                 # metric where the cause acts
    segment: dict[str, str]
    first_change_ts: datetime | None
    explained_share: float         # 0..1 of the entry delta
    status: HypothesisStatus
    evidence_for: list[Evidence] = []
    evidence_against: list[Evidence] = []

class Contribution(BaseModel):
    factor: str                    # metric name or segment label
    kind: Literal["formula", "mix", "rate", "segment", "planned", "residual"]
    delta: float
    share: float                   # signed share of entry delta; all rows sum to 1.0 ± 0.02

class TrailStep(BaseModel):
    id: str
    investigation_id: str
    n: int                         # 1, 2, 3, ...
    real_ts: datetime
    actor: Actor
    action: StepAction
    tool: str | None = None
    input: dict = {}
    output_summary: str            # max 500 chars, human readable
    output: dict | None = None     # full structured result (may be large)
    reason: str = ""               # why the actor took this step
    duration_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

class InvestigationResult(BaseModel):
    investigation_id: str
    entry: Anomaly
    verdict: Verdict
    summary: str                   # 2–4 sentences
    root_cause: Hypothesis | None
    hypotheses: list[Hypothesis]
    contributions: list[Contribution]
    loop: list[str] | None = None  # node cycle when a feedback loop was found
    trigger: str | None = None     # hypothesis id of the first change
    affected_actors: list[str] = []   # seller ids or tiers
    affected_sellers: list[str] = []
    open_questions: list[str]
    suggested_actions: list[str]
    steps_used: int
    stop_reason: Literal["confirmed", "budget", "innocent", "data_issue", "error"]
```

## 6. Incidents

```python
class Incident(BaseModel):
    id: str
    fingerprint: str               # see 09-core-agents.md §5
    title: str                     # < 90 chars
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
    investigation_ids: list[str]
    open_questions: list[str]
    suggested_actions: list[str]
    related_incident_ids: list[str] = []
    merged_into: str | None = None
    notifications: list[dict] = []  # {to, ts, ok, task_id}
    normal_ticks: int = 0           # lifecycle counter
```

Trail steps are stored separately and fetched by `investigation_id`.

## 7. Events (SSE `GET /api/events`)

```python
class Event(BaseModel):
    type: Literal["tick", "anomaly", "sentinel_decision", "investigation_started",
                  "trail_step", "investigation_finished", "incident_upserted",
                  "graph_changed", "scenario_changed", "agent_status"]
    sim_ts: datetime
    payload: dict                  # the matching model as JSON
```

## 8. A2A payloads

Each A2A message carries one JSON data part with these models, plus one short text part for humans.

```python
class SpecialistRequest(BaseModel):
    investigation_id: str
    question: str                  # one specific question
    metric: str
    segment: dict[str, str]
    window_start_ts: datetime
    window_end_ts: datetime
    baseline_start_ts: datetime
    baseline_end_ts: datetime
    context: str                   # what is known so far, max 1500 chars

class Finding(BaseModel):
    claim: str
    numbers: dict[str, float | str]
    confidence: float              # 0..1
    supports: list[CauseType] = []
    refutes: list[CauseType] = []

class SpecialistResponse(BaseModel):
    agent: str
    answered: bool
    findings: list[Finding]
    suggested_next: list[str] = []
    data_used: list[str] = []      # endpoint names it called

class SellerAlert(BaseModel):      # core → SellerOS
    incident_id: str
    seller_ids: list[str]
    skus: list[str]
    summary: str
    recommended_action: str

class SellerAlertAck(BaseModel):   # SellerOS → core
    received: bool
    actions_taken: list[str]       # e.g. "paused repricer for S-14 on SKU-E03"
```

## 9. Cartographer

```python
class AddMetricRequest(BaseModel):
    name: str
    description: str
    sql: str | None = None
    expression: str | None = None  # exactly one of sql / expression
    unit: str | None = None

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
    trail_investigation_id: str    # Cartographer steps use the same TrailStep model
```

## 10. Scenarios

```python
ScenarioId = Literal["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "I1", "I2"]

class ScenarioState(BaseModel):
    id: ScenarioId
    title: str
    active: bool
    injected_ts: datetime | None
    cleared_ts: datetime | None
```

## 11. Mock files

`web/mock/` holds one JSON file per API response in `13-api-and-ui.md`, valid against these models.
Task T00 generates them from small factory functions in `tests/factories.py`, so the mocks stay valid when contracts change.
