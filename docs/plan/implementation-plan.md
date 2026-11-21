# Implementation plan (24 hours, 3 people, Devin)

Specs: `../README.md`. Task cards: `../tasks/README.md`.

## 1. Team

| Person | Lane | Owns tasks | Main job during the day |
|---|---|---|---|
| Dev 1 | Data & Graph | T01–T10, T23 (harness part), T26 | start Devin sessions for the lane, review PRs, merge, run checkpoints for data |
| Dev 2 | Agents & Interfaces | T00, T11–T20, T24 | start Devin sessions for the lane, review PRs, merge, own `main` and the integration |
| P3 | Product & Demo (non-technical) | T21, T22, T23 (oracle part), T25 | scenario truth, domain notes for prompts, UI with an AI tool on mocks, acceptance testing, pitch, video |

P3 does not need to code by hand. T21/T22 are done by a Devin session (or Claude Code) that P3 starts and checks in the browser. P3 reviews the result against the visual rules in `../specs/13-api-and-ui.md`, not the code.

## 2. How we use Devin

1. **Setup (Dev 2, hour 0):** connect the GitHub repo to Devin. In Devin's repo setup: install `uv`, run `uv python install 3.13`, `uv sync`. Add the secret `ANTHROPIC_API_KEY` in Devin's secrets (never in the repo). Devin reads `AGENTS.md` at the repo root automatically.
2. **One task card = one Devin session = one PR.** Start a session with this prompt:

   ```
   Read AGENTS.md. Then implement docs/tasks/<card>.md exactly.
   Work on branch task/<card-id>-<slug>. Change only the files listed under "Files you own".
   Run every command under "Done when" and paste the output in the PR description.
   If the spec is unclear, choose the reversible option, write it under "Assumptions" in the PR, and continue.
   Open a PR to main when done.
   ```
3. **Parallel sessions:** each developer runs 2–3 Devin sessions at the same time, only for cards whose dependencies are merged (§4).
4. **Review:** the lane owner reviews each PR within 20 minutes: run the "Done when" commands locally, read the diff for scope, merge with squash. A PR that touches files outside its card goes back to Devin with one sentence.
5. **Conflicts:** `contracts.py`, `pyproject.toml`, `config/settings.yaml` change only in T00. If a later task needs a contract change, the developer makes it by hand on `main` first, then tells running sessions to rebase. This includes adding `S8` and `I3` to `ScenarioId` before T27.
6. **Live LLM runs:** Devin sessions run unit and replay tests only. Live evals run on a developer laptop at the checkpoints (they cost money and need the shared key).

## 3. Timeline

| Hours | Dev 1 (Devin sessions) | Dev 2 (Devin sessions) | P3 | Gate |
|---|---|---|---|---|
| 0–1 | Devin repo setup check; read specs 03–07 | **T00** scaffold + contracts + mocks (do it with Claude Code or Devin; review fast) | read 00, 04; start oracle draft | T00 merged |
| 1–3 | **T01** | **T11**, **T12** | **T25 part A**: domain notes for 6 agent prompts; **T23 oracle** draft | |
| 3–6 | **T02**, **T03**, **T06** (after T01) | **T15**, **T19** | **T21** UI on mocks (Devin session) | |
| 6 | | | | **Checkpoint 1**: `eye-sim generate --play "I1@0,S7@3"` builds data; `query_metric` works; runtime tests pass; UI mock renders |
| 6–9 | **T04**, **T07**, **T08** | **T13**, **T16**, **T18** | finish T21; test oracle wording against the sim data (with Dev 1) | |
| 9–12 | **T09**, **T05** | **T14**, **T17** | **T22** UI live wiring (after T18) | |
| 12 | | | | **Checkpoint 2**: S3 and S7 end to end live; trail in UI; I1 innocent |
| 12–15 | **T10**, **T27**, **T23 harness** | **T20**, **T24** | acceptance run of S1–S4, S7 with the harness; log failures as GitHub issues | |
| 15–17 | **T26** deployment; fix data issues | fix agent issues (prompts, stop rules) | T25 part B: slides | |
| 17 | | | | **Checkpoint 3**: all acceptance criteria (`../specs/00-overview.md` §8) pass once; `eval_scenarios.py --all` table; D6 (live demo) passes |
| 17–20 | tune scenario sizes; record replay fixtures; deploy on demo laptop + Codespaces | prompt tuning; README | slides, demo script rehearsal 1 | |
| 20–22 | **All**: rehearsals 2 and 3, record backup video, freeze `main` (tag `demo-v1`) | | | demo frozen |
| 22–24 | buffer / sleep | buffer / sleep | buffer / sleep | |

## 4. Dependency graph

```
T00 ─┬─ T01 ─┬─ T02 ─┬─ T05
     │       │       └─ T17
     │       ├─ T03
     │       ├─ T04 (needs T02, T03) ─── T27 (S8/I3, live demo)
     │       ├─ T06 ─┬─ T07 ─┬─ T13 (needs T11, T12)
     │       │       │       └─ T14 (needs T08, T11, T15, T16)
     │       │       ├─ T08 ─── T09 ─── T10 (needs T11)
     │       │       └─ T15 ─── T16 (needs T11)
     │       └─ T23 harness (needs T18)
     ├─ T11 ─── T20 (needs T19)
     ├─ T12 ─── T18 (needs T06, T13) ─┬─ T22 (needs T21)
     ├─ T19                           └─ T24
     ├─ T21 (mocks only)
     └─ T26 (needs T18, T17, T16, T19)
```

Critical path: T00 → T01 → T06 → T07 → T14 → Checkpoint 2. Dev 1 starts T06 as soon as T01 is merged.

## 5. Checkpoint commands

```bash
# Checkpoint 1
uv run eye-sim generate --days 28 --seed 42 --play "I1@0,S7@3" && uv run eye-sim stats
uv run pytest -m "not live" -q

# Checkpoint 2
./scripts/dev.sh            # starts everything
uv run python scripts/eval_scenarios.py --scenario D5 --mode live
uv run python scripts/eval_scenarios.py --scenario S3 --mode live

# Checkpoint 3
uv run python scripts/eval_scenarios.py --all --mode live --record-dir tests/fixtures/llm/
uv run python scripts/eval_scenarios.py --scenario D6 --mode live
```

## 6. Cut order (cut from the top when late)

1. MCP resources (`12-mcp-server.md` §3).
2. Optional ambiguous metric `delivery_delay_hours`.
3. Hijacker in S6 (keep the price loop, stockouts, CPC).
4. Chart expected band in the UI.
5. UI chat (keep MCP for Claude Code / Claude Desktop).
6. I2 as a separate scenario (keep the late-partition hook: S8 uses it).
7. S6 emergent from seller rules → S6 as scripted curves with the same timeline.
8. Codespaces backup (keep the laptop + tunnel).

Never cut: S8, I3 and run D6 (the live demo), S1–S5, S7, I1, S6 in some form, `add_metric`, SellerOS over A2A, the MCP server, the trail in the UI.

## 7. Risks

| Risk | Sign | Action |
|---|---|---|
| a2a-sdk 1.x API differs from expectations | T15/T16 stuck > 1 h | use the SDK's own example server as base; if still stuck, implement the JSON-RPC `message/send` + Agent Card by hand with FastAPI (keep the A2A wire format) |
| LLM investigations too slow for the demo | > 90 s per investigation | `pause_sim_during_investigation: true`; lower Detective effort to `medium`; record + replay for the demo |
| Detective picks wrong cause | eval fails | improve tool summaries first (clear numbers), then the prompt method; never change the oracle to pass |
| S6 dynamics unstable (price collapse in baseline or no effect) | T05 tuning targets missed | tune repricer thresholds; fall back to cut item 7 |
| API cost | `/api/status` tokens | `llm.max_calls_per_hour`; replay mode in rehearsals |
| Merge conflicts from parallel Devin sessions | conflicting PRs | strict "Files you own"; contract changes only on `main` by hand |
| Free hosting not available | tunnel or Codespaces fails | run on the laptop, show `localhost`; the judges see the same thing |
