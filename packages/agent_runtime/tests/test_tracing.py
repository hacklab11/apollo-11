from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from agent_runtime import extract_trace_context, inject_trace_context

provider = TracerProvider()
tracer = provider.get_tracer("test")


def test_traceparent_round_trips_through_a2a_metadata() -> None:
    with tracer.start_as_current_span("orchestrator") as parent:
        carrier: dict[str, str] = {}
        inject_trace_context(carrier)
    trace_id = format(parent.get_span_context().trace_id, "032x")
    assert carrier["traceparent"].split("-")[1] == trace_id

    with tracer.start_as_current_span(
        "agent", context=extract_trace_context(carrier)
    ) as child:
        assert child.get_span_context().trace_id == parent.get_span_context().trace_id
    assert trace.get_current_span() is trace.INVALID_SPAN


def test_no_traceparent_without_active_span() -> None:
    carrier: dict[str, str] = {}
    inject_trace_context(carrier)
    assert "traceparent" not in carrier
