# Discovery & Experience: exposure through payment

Implementation: `packages/specialists/src/eye/specialists/discovery.py`;
A2A endpoint: `/discovery`. This read-only specialist uses PydanticAI through `agent_runtime`.

Separate exposure/relevance changes from funnel breakage:
search quality and conversion, feed impressions/clicks/availability, carts → checkout →
payment attempts → successes. Compare platform, country, component version and timing.

Client errors or a component rollout need a matching funnel residual and unaffected controls.
PSP latency/failure is distinct from client-side checkout failure.
High exposure to unavailable items can hurt outcomes despite healthy overall traffic.
Reported payment p95 is attempt-weighted across source summaries, not a pooled raw-event percentile.

Use search_quality, feed_exposure, checkout_funnel, payments, errors, app_components,
metric series and change-log tools. No component rollback or payment action is available here.
See [specialists](../specs/specialists.md).
