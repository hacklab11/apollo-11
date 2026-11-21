# SellerOS

This is an independent project; run `uv run --project external/selleros selleros`
from the repository root. It does not import the core application.

Data access and actuation use the `SellerStore` protocol. The default
`FileSellerStore` re-reads `data/sellers.json` from the simulator and persists
applied pauses and price reverts to `data/actions.jsonl` (flushed and fsynced).
State is recovered from that journal on restart; the simulator does not consume
these actions. Real integrations can supply their own store to `create_app`.

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

Run the standalone offline suite with:

```sh
uv run --project external/selleros pytest external/selleros/tests -m "not live" -q
```
