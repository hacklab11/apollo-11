# Skeptic: challenge innocence with measurements

Implementation: `packages/agents/src/eye/agents/skeptic.py`; A2A skill: `challenge`.
Independently test the anomaly/hypothesis against freshness, planned changes and seasonality.

- Late or incomplete data can make a business verdict untrustworthy: report data_issue.
- A release name is not an effect plan. Check scope, timing, direction and magnitude.
- A full planned-effect match requires the expected direction and size within ±25%.
  A partial match leaves a residual; an opposite-direction effect explains nothing.
- Use same-hour baselines and comparable controls. Repeated calendar patterns do not excuse
  a new segment-specific discontinuity.

Return innocent/partly_innocent/not_innocent/data_issue with explained share and tool evidence.
An independent challenge need not agree with Detective and must not invent supporting numbers.
See [core agents](../specs/core-agents.md).
