# Core agents

Implementation: `packages/agents/src/eye/agents/`; prompts: `packages/llm/src/eye/llm/prompts/`.
Each core agent is served by `eye-agent <name>` over A2A, uses typed PydanticAI output and
`agent_runtime`, and keeps model history private. Data tools use the warehouse API.

## Sentinel — `detect`

`sentinel.py` combines deterministic point/divergent scans, prefetches freshness, baseline,
events and prior watch state, then asks the model to triage every candidate:
investigate, attach, watch, defer or ignore. Completeness is checked before declaring loss.
Prompts require watching borderline z (2.5–3) or isolated spikes and escalating repeated watches.
Open-incident context avoids duplicate investigations; a planned event is not automatic innocence.

## Detective — `investigate`

`detective.py` owns a typed hypothesis board, contribution trail, graph/segment/formula/mix tools
and specialist delegation. Skeptic is called over A2A, not imported.
Submission validation requires a confirmed root to explain at least 70% by default,
with two independent evidence groups; another unrefuted hypothesis explaining at least 20%
blocks confirmation. Mixed verdicts need at least two supported hypotheses with sufficient
combined contribution and valid attribution. Unsupported/unfinished work remains a hypothesis,
not a fabricated confirmed answer. Default investigation cap is 15 steps.
Exact stop rules live in `_stop_failures`; thresholds come from `DetectiveSettings`.

## Skeptic — `challenge`

`skeptic.py` independently tests freshness, plans and seasonality. It can return
data_issue, innocent, partly_innocent or not_innocent. Planned effects must match direction
and size (±25% for a full match); coincident releases without effect plans prove nothing.
Controls and unexplained residuals can reject innocence.

## Reporter — `report`

`reporter.py` writes a short title/summary with no tools. Its output validator rejects
numbers absent from the supplied investigation; one retry is allowed. Failure uses a deterministic
template. Runtime caps are three steps, 8,192 tokens and 30 seconds.
Reporter does not decide recovery or close incidents: Core owns lifecycle transitions.

[Cartographer](cartographer.md) is the fifth core service.
