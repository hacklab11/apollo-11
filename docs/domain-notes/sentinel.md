# Sentinel: triage, not diagnosis

Implementation: `packages/agents/src/eye/agents/sentinel.py`; A2A skill: `detect`.
The point and divergent-segment scans are deterministic. Read totals and segment shares together:
healthy GMV can hide a loss of small-third-party share.

- Check freshness before business impact. Incomplete partitions warrant defer, not a root cause.
- Prefer measured persistence and EUR/hour impact to the largest-looking chart.
- Use watch for borderline/one-hour signals and reconsider on the next tick.
- Attach related symptoms to existing incidents using segment/metric-family/graph context.
- A planned campaign is context, not permission to ignore a signal; Skeptic quantifies its effect.

Output investigate/attach/watch/defer/ignore with the supplied anomaly and evidence.
Do not invent an incident ID or run the investigation inside triage.
See [core agents](../specs/core-agents.md).
