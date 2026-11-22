# Prompts and specialist LLM compatibility

`packages/llm/src/eye/llm/` owns shared prompt resources plus the specialist inference
runtime/client. Core PydanticAI agents load these prompt files but execute through
[`agent_runtime`](agent-runtime.md), not the specialist loop.

- `runtime.py` defines `AgentSpec`/`ToolSpec`, validates tool arguments and terminal submissions,
  bounds the loop and emits evidence trail steps. Growth, Marketplace and Discovery use it.
- `client.py:LLMClient` supports Anthropic Messages and OpenAI-compatible Chat Completions,
  process-local call budgets, provider retries/quota stops and live/record/replay modes.
  Configuration is `LlmSettings`; provider credentials are read from environment, never YAML values.
- Replay fingerprints include request identity and, for compatible providers, provider/base URL.
  Missing recordings must not silently become live calls. Recorded content can contain sensitive
  data: do not commit production captures or use recording as a substitute for redaction.
- `chat_context.py` compacts structured tool context for compatible-provider transport;
  its name does not imply a separate chat service or UI.
- `prompts/` contains the active core and specialist instructions. Prompt changes are behavioral
  changes and require offline regression/eval evidence.

These modules remain reachable through specialists. Do not remove them or describe them as the
core-agent harness, and do not assume the shared runtime's full production controls apply here.
See [specialists](specialists.md), [testing](testing.md) and [production criteria](agent-criteria.md).
