"""W3C trace context propagation and run-level OTel spans and metrics."""

from collections.abc import MutableMapping

from opentelemetry import metrics, propagate, trace
from opentelemetry.context import Context

INSTRUMENTATION_NAME = "agent_runtime"

tracer = trace.get_tracer(INSTRUMENTATION_NAME)
meter = metrics.get_meter(INSTRUMENTATION_NAME)

end_state_counter = meter.create_counter(
    "agent.end_state", description="Agent runs by end state"
)
loop_counter = meter.create_counter(
    "agent.loop_detected", description="Runs stopped by loop detection"
)
cost_histogram = meter.create_histogram(
    "agent.cost", unit="USD", description="Model cost per agent run"
)


def inject_trace_context(carrier: MutableMapping[str, str]) -> None:
    """Add `traceparent` (and `tracestate`) for the current span to outgoing headers or A2A metadata."""
    propagate.inject(carrier)


def extract_trace_context(carrier: MutableMapping[str, str]) -> Context:
    """Context to start a server-side span under, from incoming headers or A2A metadata."""
    return propagate.extract(carrier)
