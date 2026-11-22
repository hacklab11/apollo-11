"""Shared time windows and bounded data reads for investigations."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import AwareDatetime, BaseModel, Field

from eye.contracts import Anomaly, Dimension
from eye.tools.models import (
    BaselineCompareInput,
    BaselineCompareOutput,
    CheckFreshnessInput,
    CheckFreshnessOutput,
    CompareSegmentsInput,
    CompareSegmentsOutput,
    ListEventsInput,
    ListEventsOutput,
)
from eye.warehouse import operations as ops
from eye.warehouse.queries import DataQueries

_HOUR = timedelta(hours=1)


class _MetricInput(BaseModel):
    metric: str
    segment: dict[Dimension, str] = Field(default_factory=dict)


class _ControlInput(_MetricInput):
    dim: Dimension
    value_a: str
    value_b: str


class _EventsInput(BaseModel):
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    system: str | None = None
    kind: str | None = None
    country: str | None = None
    planned: bool | None = None


@dataclass(frozen=True)
class _Window:
    start: datetime
    end: datetime

    @classmethod
    def from_anomaly(cls, anomaly: Anomaly) -> "_Window":
        end = anomaly.ts + _HOUR
        return cls(end - anomaly.window_hours * _HOUR, end)

    @property
    def lookback(self) -> datetime:
        return self.start - timedelta(hours=24)

    def bounded(
        self, start: datetime | None, end: datetime | None
    ) -> dict[str, datetime]:
        start = start or self.lookback
        end = end or self.end
        if not self.lookback <= start < end <= self.end:
            raise ValueError("Use a time range within evidence_start and window_end")
        return {"start": start, "end": end}

    def bounds(self) -> dict[str, datetime]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class _WindowTools:
    data: DataQueries
    window: _Window

    def baseline(self, args: _MetricInput) -> BaselineCompareOutput:
        return self.data.read(
            ops.BASELINE,
            BaselineCompareInput(
                name=args.metric, segment=args.segment, **self.window.bounds()
            ),
        )

    def freshness(self, args: _MetricInput) -> CheckFreshnessOutput:
        return self.data.read(
            ops.FRESHNESS,
            CheckFreshnessInput(**args.model_dump(), **self.window.bounds()),
        )

    def controls(self, args: _ControlInput) -> CompareSegmentsOutput:
        segment = {dim: value for dim, value in args.segment.items() if dim != args.dim}
        return self.data.read(
            ops.COMPARE,
            CompareSegmentsInput(
                **args.model_dump(exclude={"segment"}),
                segment=segment,
                **self.window.bounds(),
            ),
        )

    def events(
        self, args: _EventsInput, *, planned_only: bool = False
    ) -> ListEventsOutput:
        filters = args.model_dump(exclude={"start", "end", "planned"})
        return self.data.read(
            ops.EVENTS,
            ListEventsInput(
                **filters,
                **self.window.bounded(args.start, args.end),
                planned=True if planned_only else args.planned,
            ),
        )
