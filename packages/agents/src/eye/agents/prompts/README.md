# Agent prompts

Core prompts live here; specialist prompts live in `eye.specialists/prompts/`.
Keep one Markdown file per agent, under 1,500 words. Use these headings:

- Role
- What you know about the marketplace
- Your tools and when to use each one
- Rules
- How to finish

Use direct instructions, evidence before claims, numbers in claims, and explicit
uncertainty. Explain non-obvious rules briefly. Keep prompts stable: timestamps
and investigation IDs belong in user messages, not system prompts.

Core agents and specialists load these resources into PydanticAI agents and execute
through `agent_runtime.run_agent`. Tools and structured outputs use Pydantic schemas;
runtime limits, budgets and explicit end states bound execution. Specialists retain
the `submit` output-tool name and an eight-model-step cap.

Configure provider, model/fallback routes, effort, prices and per-response token limits
in `llm` settings. Anthropic credentials come from `ANTHROPIC_API_KEY`; OpenAI-compatible
credentials come from the environment variable named by `llm.api_key_env` (default
`OPENROUTER_API_KEY`). Never put keys in settings or prompts.

Offline tests use PydanticAI `TestModel`/`FunctionModel`, with model requests disabled.
Run `uv run python scripts/agent_evals.py` for offline evaluations. Only the separate
capped nightly workflow opts into real provider calls with `--live`.
