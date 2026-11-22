"""Independent copies of the A2A boundary contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

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
