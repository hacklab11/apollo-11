# Observability

Compose runs `grafana/otel-lgtm:0.22.0`, with Grafana on http://localhost:3000.
Services export OTLP to `http://lgtm:4318`; metric export interval is five seconds.
Tempo stores traces, Loki logs and Prometheus metrics inside the LGTM stack.

`packages/agent_runtime/src/agent_runtime/tracing.py` configures Logfire locally
(`send_to_logfire=False`), PydanticAI/httpx instrumentation and W3C trace propagation.
ASGI instrumentation avoids capturing request headers/bodies. Email/phone patterns and
sensitive-name fields are scrubbed before export. Without an OTLP endpoint, setup is inert.

Run spans include agent/run/tenant identity and end state. Metrics:
`agent.cost` (USD histogram), `agent.loop_detected`, and `agent.end_state{status}`.
A2A metadata and outbound HTTP carry trace context so delegation can be followed across services.
The control plane's hash-chained audit log is separate from telemetry.

Grafana provisioning is in `config/grafana/alerting/rules.yaml`:
rules evaluate each minute and require five minutes over threshold.
Cost rate and loop rate thresholds are 0.01; failed/gave_up fraction is 10%;
needs_input fraction is 10%. These are local demo thresholds, not formal SLOs.
No-data is OK; evaluation errors are errors. Alert rules alone do not establish
delivery to an on-call system. Replayable per-step I/O and production SLOs remain
gaps documented in [criteria](agent-criteria.md).
