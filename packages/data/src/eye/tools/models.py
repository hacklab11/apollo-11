"""Schemas for deterministic tool calls."""

from datetime import datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from eye.contracts_v2 import Anomaly, Dimension


class WindowInput(BaseModel):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.start >= self.end:
            raise ValueError("start must precede end")
        return self


class MetricWindowInput(WindowInput):
    metric: str
    segment: dict[Dimension, str] = Field(default_factory=dict)


class CompareWindowInput(MetricWindowInput):
    base_start: AwareDatetime | None = None
    base_end: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_baseline(self) -> Self:
        if self.base_start is None and self.base_end is None:
            return self
        if (
            self.base_start is None
            or self.base_end is None
            or self.base_start >= self.base_end
        ):
            raise ValueError("baseline requires base_start before base_end")
        return self


class ToolOutput(BaseModel):
    summary: str = Field(max_length=300)


class BaselineCompareInput(WindowInput):
    name: str
    segment: dict[Dimension, str] = Field(default_factory=dict)


class BaselinePoint(BaseModel):
    ts: datetime
    actual: float | None
    expected: float | None
    spread: float | None
    lower: float | None
    upper: float | None
    z: float | None
    completeness: float


class BaselineCompareOutput(ToolOutput):
    points: list[BaselinePoint]
    actual: float | None
    expected: float | None
    delta: float | None


class ScanAnomaliesInput(BaseModel):
    ts: AwareDatetime


class ScanDivergentSegmentsInput(ScanAnomaliesInput):
    window_hours: int = Field(default=6, ge=1)


class ScanOutput(ToolOutput):
    anomalies: list[Anomaly]


class DrillDownInput(CompareWindowInput):
    dim: Dimension


class SegmentComparison(BaseModel):
    value: str
    actual: float | None
    expected: float | None
    delta: float | None
    delta_pct: float | None
    z: float | None


class DrillDownRow(SegmentComparison):
    share_of_total_delta: float | None
    focus: bool


class DrillDownOutput(ToolOutput):
    rows: list[DrillDownRow]


class CompareSegmentsInput(DrillDownInput):
    value_a: str
    value_b: str


class CompareSegmentsOutput(ToolOutput):
    a: SegmentComparison
    b: SegmentComparison


class FirstChangeInput(WindowInput):
    metrics: list[str]
    segment: dict[Dimension, str] = Field(default_factory=dict)


class FirstChangeOutput(ToolOutput):
    changes: dict[str, datetime | None]


class CheckFreshnessInput(WindowInput):
    tables: list[str] = Field(default_factory=list)
    metric: str | None = None
    segment: dict[Dimension, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if bool(self.tables) == bool(self.metric):
            raise ValueError("provide tables or a metric, not both")
        return self


class FreshnessPoint(BaseModel):
    ts: datetime
    completeness: float


class CheckFreshnessOutput(ToolOutput):
    points: list[FreshnessPoint]
    late_hours: list[datetime]
    verdict: Literal["ok", "late", "incomplete"]


class ListEventsInput(WindowInput):
    system: str | None = None
    kind: str | None = None
    country: str | None = None
    planned: bool | None = None


class Event(BaseModel):
    id: str
    ts: datetime
    source: Literal["change_log", "external_status"]
    system: str
    kind: str
    planned: bool
    scope: dict = Field(default_factory=dict)
    description: str
    expected_effect: dict[str, str | float] = Field(default_factory=dict)
    status: str | None = None


class ListEventsOutput(ToolOutput):
    events: list[Event]


class PlannedEffectCheckInput(MetricWindowInput):
    event_id: str


class PlannedEffectCheckOutput(ToolOutput):
    planned_effect: float
    observed_effect: float | None
    explained_share: float
    verdict: Literal["matches", "exceeds", "below", "opposite"]
