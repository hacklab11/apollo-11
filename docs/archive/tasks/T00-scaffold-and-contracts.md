# T00 Scaffold, contracts, config, mocks

Owner: Dev 2 · Depends on: – · Blocks: all

## Goal
A runnable empty project with all shared contracts and config, so that all other cards can work in parallel.

## Read first
`AGENTS.md`, `docs/specs/architecture.md`, `docs/specs/contracts.md`, `docs/specs/web.md` §1.

## Files you own
`pyproject.toml`, `uv.lock`, `.gitignore`, `.python-version`, `config/settings.yaml`, `src/eye/__init__.py`, `src/eye/config.py`, `src/eye/contracts.py`, empty package dirs from `architecture.md` §2 (with `__init__.py`), `tests/conftest.py`, `tests/factories.py`, `tests/test_contracts.py`, `tests/test_config.py`, `web/mock/*.json`, `scripts/make_mocks.py`, `scripts/dev.sh` (stub), `README.md` (stub).

## Do not touch
`docs/`, `AGENTS.md`.

## Steps
1. `pyproject.toml`: package `eye` (src layout), `requires-python = ">=3.13"`, pinned deps from `architecture.md` §4, dev deps `pytest`, `pytest-asyncio`, `ruff`. Console scripts: `eye-core`, `eye-sim`, `eye-specialists`, `eye-mcp` (point to stub `main()` functions that print "not implemented" and exit 1).
2. pytest marker `live` registered; default run excludes nothing (CI uses `-m "not live"`).
3. `.gitignore`: `data/`, `.env`, `external/selleros/data/`, `.venv/`, `__pycache__/`.
4. `contracts.py`: every model and enum in `contracts.md`, exactly. Add `summary: str` to nothing here (that is for tool outputs).
5. `config.py` + `config/settings.yaml`: every key in `architecture.md` §5 with type, range validation, default. `load_settings(path) -> Settings` (pydantic). Invalid value → `ValueError` naming the key and the valid range.
6. `tests/factories.py`: small functions that build valid example objects (anomaly, incident for S7 and S6, trail of 10 steps, graph of 12 nodes, metric series of 48 h with a drop).
7. `scripts/make_mocks.py`: writes every mock file listed in `web.md` §1 from the factories. Run it and commit the files.
8. `README.md`: one paragraph + "see docs/README.md".

## Done when
```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest -m "not live" -q          # test_contracts: each mock file validates against its model; test_config: defaults load, bad range raises
uv run python scripts/make_mocks.py && git diff --exit-code web/mock   # mocks are up to date
```

## Out of scope
Any simulator, agent, or API logic.
