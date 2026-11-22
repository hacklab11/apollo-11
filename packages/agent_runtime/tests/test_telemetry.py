import os
import subprocess
import sys
import textwrap
from unittest.mock import Mock

import pytest

from agent_runtime import tracing


def test_no_endpoint_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    configure = Mock()
    monkeypatch.setattr(tracing.logfire, "configure", configure)
    assert tracing.setup_telemetry() is False
    app = Mock()
    assert tracing.instrument_app(app) is app
    configure.assert_not_called()


def test_configuration_error_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.setattr(
        tracing.logfire, "configure", Mock(side_effect=ValueError("bad config"))
    )
    with pytest.raises(ValueError, match="bad config"):
        tracing.setup_telemetry()
    assert not tracing._configured


def test_setup_exports_scrubbed_model_spans_once() -> None:
    script = textwrap.dedent("""
        import asyncio
        import json
        from unittest.mock import patch
        import logfire
        from opentelemetry import trace
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from pydantic_ai import Agent, models
        from pydantic_ai.models.test import TestModel
        from agent_runtime.tracing import setup_telemetry

        models.ALLOW_MODEL_REQUESTS = False
        exporter = InMemorySpanExporter()
        configure = logfire.configure
        def capture(**kwargs):
            return configure(**kwargs, additional_span_processors=[SimpleSpanProcessor(exporter)])
        with patch.object(logfire, 'configure', side_effect=capture) as configured:
            assert setup_telemetry()
            assert setup_telemetry()
            assert configured.call_count == 1
        with trace.get_tracer('test').start_as_current_span('root') as span:
            span.set_attribute('customer_name', 'Alice Example')
            span.set_attribute('message', 'Contact alice@example.com at +1 (415) 555-1234')
            span.set_attribute('password', 'hidden-value')
            asyncio.run(Agent(TestModel(custom_output_text='done')).run('Email alice@example.com'))
        spans = exporter.get_finished_spans()
        payload = json.dumps([dict(s.attributes) for s in spans])
        for sensitive in ['alice@example.com', '415', 'Alice Example', 'hidden-value']:
            assert sensitive not in payload, payload
        assert 'Contact [redacted] at [redacted]' in payload
        assert any('gen_ai.operation.name' in s.attributes for s in spans), payload
        assert len({s.context.trace_id for s in spans}) == 1
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env={
            **os.environ,
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
            "OTEL_TRACES_EXPORTER": "none",
            "OTEL_METRICS_EXPORTER": "none",
            "OTEL_LOGS_EXPORTER": "none",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
