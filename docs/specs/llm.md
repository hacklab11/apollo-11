# Prompts and model configuration

`packages/llm/src/eye/llm/prompts/` owns shared core-agent and specialist prompt
resources only. All agents execute through PydanticAI and [`agent_runtime`](agent-runtime.md).

- `agent_runtime/configured_models.py` builds Anthropic or OpenAI-compatible models,
  explicit fallback chains and pinned price tables from structural configuration.
- `LlmSettings` selects provider, base URL, credential environment variable, models,
  fallback models, prices, effort and per-response token limit. Credentials never belong in YAML.
- Runtime limits and propagated budgets cap steps, tools, tokens, cost and elapsed time.
  Runtime end states distinguish completion, missing input, failure and exhausted limits.
- Offline tests use `TestModel`, `FunctionModel` or mock provider transports with real
  model requests disabled. Offline evals replay scripted responses, not network recordings.
- Prompt changes are behavioral changes and require offline regression/eval evidence.

There is no service-process record/replay mode or hourly-call quota. `/api/status` retains
the compatibility field `llm_mode="live"`; model execution settings are configured at startup.
See [specialists](specialists.md), [testing](testing.md) and [production criteria](agent-criteria.md).
