"""Static references for framework hooks, wire fields and supported library APIs.

Vulture matches names, not qualified symbols. Keep entries explicit: no wildcard
name exclusions and no automatic regeneration from Vulture's findings.
"""

# ruff: noqa: B018

from typing import Any

_: Any = None

# Console scripts are resolved from workspace pyproject.toml entry points.
_.main

# Pydantic configuration and JSON fields read across HTTP/A2A boundaries.
_.model_config
_.sentinel_url
_.detective_url
_.reporter_url
_.cartographer_url
_.growth_url
_.marketplace_url
_.discovery_url
_.selleros
_.higher_is_better
_.statement
_.real_ts
_.duration_ms
_.steps_used
_.baseline_start_ts
_.baseline_end_ts
_.supports
_.refutes
_.suggested_next
_.data_used
_.actions_taken
_.trail_investigation_id
_.closed_by
_.closed_ts
_.recovered_ts
_.late_hours
_.planned_effect
_.observed_effect
_.trail_steps_total
_.data_complete_ts
_.seller_exchange
_.running
_.llm_calls
_.llm_stop_reason
_.undercut_pct
_.stock_policy_days
_.generated_for_seed
_.seller_tier

# PydanticAI AbstractCapability callbacks are dispatched by the framework.
_.before_model_request
_.after_model_request
_.wrap_output_validate

# Public library helpers exercised by their package/root tests.
_.to_metadata
_.from_metadata
_.kill_system
_.kill_tenant
_.kill_run
_.refine_causal_seeds
_.on_change
_.set_edge_status
_.set_state
_.neighbors_upstream
_.metric_value
_.budget_status
_.reverted_prices
_.release
_.add_external_status

# Local source protocol, outside Compose's HTTP path.
_.history_start
