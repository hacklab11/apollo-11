"""W3C trace context propagation and run-level OTel spans and metrics."""

import os
import re
from collections.abc import MutableMapping

import logfire
from logfire.types import StringValue
from opentelemetry import metrics, propagate, trace
from opentelemetry.context import Context
from starlette.types import ASGIApp

INSTRUMENTATION_NAME = "agent_runtime"
_configured = False


def redact_pii(item: StringValue) -> str:
    value = re.sub(
        r"[\w.+-]{1,128}@[\w.-]{1,128}\.[A-Za-z]{2,}", "[redacted]", item.value
    )
    return re.sub(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)", "[redacted]", value)


def setup_telemetry() -> bool:
    """Configure local OTLP export once; remain inert outside instrumented deployments."""
    global _configured
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    if not _configured:
        logfire.configure(
            send_to_logfire=False,
            console=False,
            distributed_tracing=True,
            scrubbing=logfire.ScrubbingOptions(
                extra_patterns=[
                    r"(?:first|last|full|person|customer)[_. -]?name",
                    "email",
                    "phone",
                ],
                transform_string=redact_pii,
            ),
        )
        logfire.instrument_pydantic_ai()
        logfire.instrument_httpx()
        loop_counter.add(0)
        _configured = True
    return True


def instrument_app(app: ASGIApp) -> ASGIApp:
    """Instrument an ASGI boundary without capturing headers or request bodies."""
    if setup_telemetry():
        return logfire.instrument_asgi(app)
    return app


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
