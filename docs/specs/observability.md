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

## Agent logs

Agent services (`eye-agent <name>`) log JSON to stdout. When an OTLP endpoint is set,
the same records go to Loki through Logfire's logging handler, linked to the active
trace. `EYE_LOG_LEVEL` sets the level: a standard Python level name, default `WARNING`.

- A failed A2A task logs `agent=<name> task_failed=<type> error=<message>`.
  The message is redacted (email and phone patterns) and cut to 1000 characters.
- The detective logs each failed tool call as a warning:
  `detective tool_failed investigation=<id> tool=<name> args=<json> error=<message>`.
- The detective logs each finished investigation:
  `detective finished investigation=<id> metric=<m> verdict=<v> stop=<s> root_cause=<node>
  hypotheses=<n> steps=<n> open_questions=<json>`. The level is WARNING when `stop`
  is `error` or `budget`, else INFO (visible with `EYE_LOG_LEVEL=INFO`).
- Core logs `A2A <skill> ended <status>: <reason>` for A2A tasks that do not finish.

## Dashboard

`config/grafana/dashboards/eye-system-health.json` ("Eye: system health", folder Eye),
provisioned by `config/grafana/dashboards.yaml`. Rows:

- Agents and investigations: runs by end state, failed/gave-up ratio, LLM cost,
  tokens per minute.
- Tools and model calls: tool errors and error ratio per tool, model latency p95 and
  calls per minute (Tempo span metrics).
- Services: HTTP rate, 5xx rate and latency p95 per service; orchestrator hour
  duration; A2A calls.
- Logs and traces: agent warnings from Loki; failed spans from Tempo.

To see why an investigation found no root cause: find its `detective finished` line,
then open the trace. `execute_tool` spans hold tool arguments and results, and `chat`
spans hold model messages.
