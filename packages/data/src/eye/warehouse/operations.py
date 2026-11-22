"""Typed operations shared by the warehouse API and its clients."""

from dataclasses import dataclass
from datetime import timedelta

from pydantic import AwareDatetime, BaseModel, Field, TypeAdapter

from eye.contracts import Dimension, GraphView, MetricDef, Series
from eye.tools.drill import AttributionResult
from eye.tools.models import (
    BaselineCompareInput,
    BaselineCompareOutput,
    CheckFreshnessInput,
    CheckFreshnessOutput,
    CompareSegmentsInput,
    CompareSegmentsOutput,
    CompareWindowInput,
    DrillDownInput,
    DrillDownOutput,
    FirstChangeInput,
    FirstChangeOutput,
    ListEventsInput,
    ListEventsOutput,
    MetricWindowInput,
    PlannedEffectCheckInput,
    PlannedEffectCheckOutput,
    ScanAnomaliesInput,
    ScanDivergentSegmentsInput,
    ScanOutput,
    WindowInput,
)


class SeriesInput(MetricWindowInput):
    group_by: list[Dimension] = Field(default_factory=list)


class GraphInput(BaseModel):
    node: str | None = None
    depth: int | None = Field(None, ge=0)


class NodeInput(BaseModel):
    node: str


class PathInput(BaseModel):
    src: str
    dst: str


class PartitionWindow(BaseModel):
    earliest: AwareDatetime | None = None
    latest: AwareDatetime | None = None


class CompleteHourInput(BaseModel):
    ts: AwareDatetime


@dataclass(frozen=True)
class Operation[T: BaseModel, R]:
    path: str
    input: type[T]
    response: type[R]
    output: TypeAdapter[R]


def operation[T: BaseModel, R](
    path: str, input: type[T], output: type[R]
) -> Operation[T, R]:
    return Operation(path, input, output, TypeAdapter(output))


SERIES = operation("series", SeriesInput, list[Series])
BASELINE = operation("baseline", BaselineCompareInput, BaselineCompareOutput)
FRESHNESS = operation("freshness", CheckFreshnessInput, CheckFreshnessOutput)
EVENTS = operation("events", ListEventsInput, ListEventsOutput)
SCAN = operation("anomalies", ScanAnomaliesInput, ScanOutput)
DIVERGENT = operation("divergence", ScanDivergentSegmentsInput, ScanOutput)
COMPARE = operation("compare-segments", CompareSegmentsInput, CompareSegmentsOutput)
DRILL = operation("drill-down", DrillDownInput, DrillDownOutput)
FIRST_CHANGE = operation("first-change", FirstChangeInput, FirstChangeOutput)
PLANNED = operation("planned-effect", PlannedEffectCheckInput, PlannedEffectCheckOutput)
DECOMPOSE = operation("decompose", CompareWindowInput, AttributionResult)
MIX_RATE = operation("mix-rate", DrillDownInput, AttributionResult)
GRAPH = operation("graph", GraphInput, GraphView)
CYCLES = operation("cycles", NodeInput, list[list[str]])
PATH = operation("path", PathInput, bool)
COMPLETE_HOUR = operation("complete-hour", CompleteHourInput, bool)
METRICS = TypeAdapter(list[MetricDef])


def enforce_query_bounds(args: BaseModel) -> None:
    # Do not change the historical LLM tool schemas (recorded responses depend on them).
    if isinstance(args, WindowInput) and args.end - args.start > timedelta(days=60):
        raise ValueError("window must not exceed 60 days")
    if (
        isinstance(args, CompareWindowInput)
        and args.base_start is not None
        and args.base_end is not None
        and args.base_end - args.base_start > timedelta(days=60)
    ):
        raise ValueError("baseline window must not exceed 60 days")
    if isinstance(args, ScanDivergentSegmentsInput) and args.window_hours > 24 * 60:
        raise ValueError("divergence window must not exceed 60 days")
