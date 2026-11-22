# Regression cases

Drop a uniquely named Dataset YAML or JSON file here. All files are discovered
recursively by `uv run python scripts/agent_evals.py`; no registry edit is needed.
Use the `cases` structure in `../s8.yaml`. Each case needs `name`, `inputs.agent`,
`inputs.payload` (model-visible context), `inputs.responses` (recorded structured
submissions), `metadata` (capture provenance), and independent `expected_output`
decision fields. Do not include credentials, prompts containing private data, or
ground-truth tables. A malformed or failing case fails the offline gate.

The suite invokes production agents against recorded model submissions and typed
data reads (or a scoped SellerOS snapshot). Keep the original scenario harness
and its fixtures for full marketplace scenario replays.
