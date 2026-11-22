# SellerOS

SellerOS runs as its own A2A service on the shared PydanticAI agent runtime.
Start it from the repository root with `uv run --package selleros selleros`.
The question agent can only read request-scoped data.

Data access and actuation use the `SellerStore` protocol. The default
`FileSellerStore` re-reads `data/sellers.json` from the simulator and persists
applied pauses and price reverts to `data/actions.jsonl` (flushed and fsynced).
State is recovered from that journal on restart. The marketplace simulator
consumes new actions before repricing: pauses disable the offer's repricer, and
reverts restore the previous price as a manual override. Real integrations can
supply their own store to `create_app`.

For a price-revert alert, SellerOS registers each eligible price change with
the orchestrator at `EYE_CORE_URL` (default `http://127.0.0.1:8000`).
Set that URL for SellerOS if Core uses a nondefault local port.
SellerOS and the simulator use OAuth client-credentials tokens with
`selleros:revert` scope and audience `eye-core`. Keep
`EYE_SELLER_REVERT_OPERATOR_TOKEN` on Core only, in the launch environment.
An operator reviews the exact offer and prices in `GET /api/approvals`, then
uses `POST /api/approvals/{id}` with `{"approve": true}` to apply it. Both
calls require the operator bearer credential or a trusted authenticated
person; direct callers cannot supply their own identity header. SellerOS
verifies the recorded human decision and current price at the tool boundary.
Pending, rejected, unavailable or stale approvals cannot change prices.
The simulator rechecks permission when consuming a queued revert, so a stop
holds the action and resuming only applies it if the price change is still current.
Legacy journal entries without approval keys stay as history and are not applied.
If delivery fails after the decision was saved, retry the original A2A alert;
the journal deduplicates by approval key across retries and restarts.

The default provider is Anthropic with `claude-opus-5-5`, using
`ANTHROPIC_API_KEY`. For OpenRouter, set these non-secret environment variables:

```sh
export SELLEROS_PROVIDER=openai_compatible
export SELLEROS_MODEL=nvidia/nemotron-3.5-lightning:free
export SELLEROS_BASE_URL=https://openrouter.ai/api/v1
export SELLEROS_API_KEY_ENV=OPENROUTER_API_KEY
```

Supply the credential separately in the named environment variable; never store
its value in configuration. `scripts/dev.sh` inherits these variables. SellerOS
has its own settings and does not automatically read core's `llm` configuration.
With OpenRouter and no explicit model, it tries GPT OSS 20B, then Qwen Flash,
then free Nemotron. Model names and prices are pinned to enforce the cost cap.

Run the standalone offline suite with:

```sh
uv run pytest packages/selleros/tests tests/api/test_public_api.py -m "not live" -q
```
