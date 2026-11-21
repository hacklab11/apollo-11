# Windows run audit (2026-11-21)

Audit of `main` at `8a87a9d` on Windows 11 (Git Bash 5.2, uv-managed CPython 3.13.1, system locale cp936).
No code was changed except adding `tzdata` for Windows in `pyproject.toml`.

Update for `8a4540a`: #55 fixes item 1 for a missing or rejected key. Startup now logs `refinement_unavailable` and continues. Refinement still runs inside the lifespan, so with a valid key the API waits for it. #55 detects the missing key by matching the `TypeError` message text, which can break when the SDK changes its wording. Items 2–5 were not addressed by #53–#56.

New blocker at `8a4540a`: #53 imports the POSIX-only `fcntl` module in `src/eye/sim/seller_actions.py:3` (used at line 56). The simulator, and therefore `eye-core`, cannot be imported on Windows. `uv run pytest -m "not live" -q` stops during collection: `ModuleNotFoundError: No module named 'fcntl'` (6 collection errors: `tests/graph`, `tests/registry`, `tests/sim`, `tests/test_eval_harness.py`, `tests/tools/test_stats.py`, `tests/tools/test_validate.py`). SellerOS has the same import in `external/selleros/selleros/data.py:3` (used at lines 140 and 183). Fix: use a cross-platform lock (`msvcrt.locking` on Windows), or drop the lock and write files with an atomic replace. Ruff check and format still pass.

## Blocks running locally

| # | Problem | Evidence | Fix |
| --- | --- | --- | --- |
| 1 | Core cannot start without a working LLM key, even with `sim.autostart: false`. Startup runs Cartographer seed refinement inside the FastAPI lifespan, before the API binds. | `api/app.py:76` → `orchestrator.py:197` → `main.py:152` → `agents/cartographer.py:566`. No key: `TypeError: "Could not resolve authentication method..."`. Bad key: `anthropic.AuthenticationError: Error code: 401`. Both exit 3. | Run refinement as a background task after startup; catch provider errors; keep the seed graph on failure. |
| 2 | Windows reserves the default ports. | `netsh interface ipv4 show excludedportrange protocol=tcp` lists 7966–8065, 8066–8165, 8166–8265. MCP and specialists failed: `[WinError 10013]` on 8100 and 8200. Only 8300 binds. | Make ports overridable by environment variable; derive A2A URLs from `ports`; stop hard-coding 8300 in `external/selleros/selleros/server.py:72,99`. |
| 3 | DuckDB queries run in the host time zone. `SET TimeZone = 'UTC'` in `sim/store.py:30` applies to the main connection only; `.cursor()` does not inherit it. | A cursor reports `Europe/Berlin` after `SET TimeZone='UTC'` on its parent. Affects `hour()`, weekday and `date_trunc('day', …)` (for example `config/metrics.yaml:492`). Setting `TZ=UTC` fixed 35 of 42 test failures. | Set the time zone on every cursor in `Store.cursor()` and `DuckDBSource`. |
| 4 | Text files are read with the locale encoding (cp936/GBK on this machine). | `agents/reporter.py:61` and 5 calls in `scripts/eval_scenarios.py` use `read_text()`/`write_text()` without `encoding`. Tests fail with `UnicodeDecodeError: 'gbk' codec can't decode byte 0x92`. `PYTHONUTF8=1` fixed these and the MCP test failures. | Pass `encoding="utf-8"` everywhere. |
| 5 | Without `tzdata`, DuckDB cannot return `TIMESTAMPTZ` values in the host zone (no IANA database on Windows). | Fixed by the `pyproject.toml` change on this branch; 28-day backfill completes. | Keep; item 3 removes the dependency on the host zone. |

## Test and lint results

- `uv run ruff check .` and `uv run ruff format --check .`: pass.
- `uv run pytest -m "not live" -q`: 41 failed, 648 passed, 173 errors. The errors are local: `%TEMP%\pytest-of-<user>` is access-denied; `--basetemp` avoids them.
- With `--basetemp`: 42 failed, 820 passed.
  - 35 fixed by `TZ=UTC` (item 3): data API, stats, validate, registry engine, S1 scenario, specialists and Detective/Sentinel replays.
  - 5 fixed by `PYTHONUTF8=1` (item 4): 2 decode errors and 3 MCP tests.
  - 2 still fail with both: `test_cartographer.py::test_replay[fee_per_order]` and `[fee_per_order_mix]`, "Replay tried to go live". B1 (#48) added views to the table list in `tools/validate.py` after the fixtures were last recorded (#43), which likely changes tool output and the replay key.

## Design debt

- **LLM numbers decide CONFIRMED.** `explained_share` and contribution values in the Detective submission come from the model and drive the 70% rule (`agents/detective.py:161,187`). Nothing recomputes them from tool output. This conflicts with "tools compute, agents decide".
- **Replay is fragile.** The cache key hashes full messages, including tool outputs (`llm/client.py:301-314`). Any tool change, or a time-zone offset in a timestamp, invalidates recordings. Replay misses fall through to live calls, so there is no offline mode.
- **Configuration depends on the working directory.** `load_settings()` reads `config/settings.yaml` with no override; `data/`, `web/` and `external/selleros/data/` are relative paths. The README asks users to edit the tracked settings file.
- **Host names.** A2A clients use `localhost` while all servers bind `127.0.0.1`. On Windows `localhost` resolves to `::1` first.
- **The UI needs the internet.** Cytoscape, Chart.js and Google Fonts load from CDNs, with no guard if they fail.
- **No CI.** 56 commits in about 5 hours with several follow-up fix PRs. `main.py:30` loads agents with an optional import, so a missing module fails quietly.
- `scripts/dev.sh` works in Git Bash (CRLF tolerated; it tears down after a crash) but is Bash-only.

Checked and fine: agent tools cannot read `scenario_log` (`tools/sqlguard.py`, `tools/stats.py`); only core opens DuckDB, other services use HTTP.

## Highest-leverage fixes

1. Start without an LLM: move seed refinement out of the lifespan (`orchestrator.py`, `main.py`). Small.
2. UTC on every DuckDB cursor (`sim/store.py`, `datasource.py`). Tiny.
3. Explicit UTF-8 for all file I/O, plus a Windows CI job. Small.
4. One source for ports and URLs with environment overrides (`config.py`, SellerOS `server.py`, `scripts/dev.sh`). Medium; touches the settings model.
5. Recompute Detective shares from the decomposition tool before the stop rule (`agents/detective.py`). Medium.
