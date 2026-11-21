# 08 Agent runtime

Code: `packages/llm/src/eye/llm/runtime.py`, prompts in `packages/llm/src/eye/llm/prompts/`. Task: T11.
All LLM agents in core, the specialists, SellerOS, and the chat use this runtime (SellerOS has its own copy, because it must not import `eye`; T17 copies the file).

## 1. API

```python
@dataclass
class ToolSpec:
    name: str
    description: str                    # 1–3 sentences: what it does, when to use it, what it returns
    input_model: type[BaseModel]        # JSON schema from pydantic; additionalProperties false
    fn: Callable[[BaseModel], BaseModel | Awaitable[BaseModel]]
    strict: bool = True

@dataclass
class AgentSpec:
    name: Actor                         # sentinel, detective, ...
    system_prompt_file: str
    tools: list[ToolSpec]
    submit_model: type[BaseModel]       # final answer schema; exposed as tool "submit"
    effort: str                         # from config llm.effort.<name>
    model: str                          # from config llm.models
    max_steps: int                      # max LLM turns
    max_tokens: int = 16000

async def run_agent(spec: AgentSpec, user_content: str, investigation_id: str,
                    on_step: Callable[[TrailStep], Awaitable[None]]) -> BaseModel
```

## 2. Loop

```
messages = [user(user_content)]
for turn in range(spec.max_steps):
    resp = llm_call(spec, messages)                 # §3 handles live/record/replay
    record TrailStep(action="llm_call", tokens, short text of any visible reasoning text blocks)
    if resp.stop_reason == "refusal": record note; raise AgentRefused
    if resp.stop_reason == "max_tokens": record note; append resp; append user("Continue. Be shorter."); continue
    append assistant(resp.content)                  # append the full content list, unchanged
    tool_uses = [b for b in resp.content if b.type == "tool_use"]
    if not tool_uses:
        append user("Call submit with your final answer."); continue
    results = run all tool_uses concurrently (asyncio.gather):
        - "submit": validate input against submit_model → return it as the result of run_agent
        - other: parse input with input_model (pydantic); call fn; result = output.model_dump_json()
        - validation error or exception → tool_result is_error=True with a short message
        - each tool call → TrailStep(action="tool_call" or "a2a_call", tool, input, output_summary=output.summary, reason=<text block before the call, if any>)
    append ONE user message with all tool_result blocks
raise BudgetExceeded  (caller turns this into verdict HYPOTHESIS, stop_reason "budget")
```

Rules from the Claude API (see `01-architecture.md` §4.1):
- `tool_choice` = auto. Do not force tools.
- Do not send `thinking` disabled or `budget_tokens`. Set `output_config={"effort": spec.effort}`.
- Send the beta `server-side-fallback-2026-07-01` with `fallbacks: "default"`.
- Put `cache_control` on the last system block. Keep the system prompt and tool list byte-stable between calls (sorted tool list, no timestamps).
- Read the official Python SDK docs (installed `anthropic` 2.12.0) for exact parameter names. If a parameter name is wrong, the SDK raises; fix it from the docs, do not guess again.
- Retries: the SDK retries 429/5xx twice. On final failure record a TrailStep note and raise `LLMUnavailable`.

## 3. Record / replay

Config `llm.mode`:
- `live`: call the API.
- `record`: call the API and append `{key, request_digest, response_json}` to `llm.cache_path`.
- `replay`: look up `key`; if found, return the stored response; if not found, fall back to live and log `replay_miss` (the demo still works).

`key` = sha256 of (model, system prompt text, tool names, the messages JSON with sorted keys). Sim timestamps are in the messages; real timestamps must not be. With the same seed and the same inject times, the tool results are the same, so the keys match.

## 4. Budgets and cost control

- Per investigation: `detective.max_steps` LLM turns for the Detective; specialists and Skeptic have their own `max_steps` (8 and 6).
- Global: `llm.max_calls_per_hour` (default 600, range 50–5000). Above it, the orchestrator skips new investigations and records `budget_paused` in `/api/status`.
- Log input/output tokens per call into the TrailStep; `/api/status` shows totals.

## 5. Prompt files

One Markdown file per agent in `packages/llm/src/eye/llm/prompts/`. Structure:

```
# Role
# What you know about the marketplace   (short; P3 writes the domain notes)
# Your tools and when to use each one
# Rules                                  (evidence before claims; numbers in every claim; say what is uncertain)
# How to finish                          (call submit; schema summary)
```

Keep each prompt under 1 500 words. Write direct instructions. Do not write "you MUST" in capitals. Give the reason for a rule in one line; the model follows reasons better than shouting.

## 6. Tests (T11)

1. Fake client (no network): a scripted sequence of responses with tool_use blocks → the loop calls the tools, sends one user message with all results, returns the submit model.
2. Tool raising an exception → `is_error` result, loop continues.
3. Invalid submit input → `is_error`, the model gets a second chance.
4. `max_steps` reached → `BudgetExceeded`.
5. Record then replay: the second run makes zero network calls (fake transport counts calls) and returns the same result.
6. Every tool call produced exactly one TrailStep with a non-empty `output_summary`.
