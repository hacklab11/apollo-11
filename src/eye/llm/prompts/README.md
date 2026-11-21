# Agent prompts

Store one Markdown file per agent here, under 1,500 words. Use these headings:

- Role
- What you know about the marketplace
- Your tools and when to use each one
- Rules
- How to finish

Use direct instructions, evidence before claims, numbers in claims, and explicit
uncertainty. Explain non-obvious rules briefly. Finish by calling the strict
`submit` tool. Keep prompts stable: timestamps and investigation IDs belong in
user messages, not system prompts.

The runtime reads `AgentSpec.system_prompt_file`, sorts tools by name, and caches
the last system block. Set model, effort, token limit, and turn limit on the spec
from configuration. The shared client reads `llm` settings lazily; applications
can install an `LLMClient` with `configure_client`. `budget_status()` exposes the
rolling hour's logical network calls, cumulative input/output tokens, and
`budget_paused` for `/api/status`. SDK retries belong to the same logical call.
Replay hits consume no network-call budget; their tokens remain observable.

Offline tests inject the installed SDK's `httpx2.AsyncClient` with
`httpx2.MockTransport`; SDK 2.12.0 does not accept `httpx.AsyncClient`.
API requests use `AsyncAnthropic(max_retries=2).beta.messages.create` with
`output_config={"effort": spec.effort}`, `tool_choice={"type": "auto"}`,
`betas=["server-side-fallback-2026-07-01"]`, and `fallbacks="default"`.
The API key is read only by the SDK from the environment.

The test-only prompt lives in `tests/llm/agent.md`. Run the opt-in live round trip
with `uv run pytest tests/llm -m live -q` after setting `ANTHROPIC_API_KEY`.
