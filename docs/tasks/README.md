# Task cards

One card = one Devin session = one PR. How to start a session: `../plan/implementation-plan.md` §2.
Status is tracked in GitHub (one issue per card, labelled with the lane). Do not track status in this file.

| Card | Title | Lane / owner | Depends on | Est. |
|---|---|---|---|---|
| [T00](T00-scaffold-and-contracts.md) | Scaffold, contracts, config, mocks | Dev 2 | – | 1 h |
| [T01](T01-sim-world-and-demand.md) | Simulator: world, demand, checkout funnel, store, CLI | Dev 1 | T00 | 2–3 h |
| [T02](T02-sim-market-and-finance.md) | Simulator: offers, repricers, Buy Box, stock, POs, finance, sellers.json | Dev 1 | T01 | 2–3 h |
| [T03](T03-sim-ads-apps-pipeline.md) | Simulator: ads, marketing, payments, client errors, change log, pipeline | Dev 1 | T01 | 2 h |
| [T04](T04-scenarios-core.md) | Scenarios S1–S5, S7, I1, I2 | Dev 1 | T02, T03 | 2–3 h |
| [T05](T05-scenario-s6.md) | Scenario S6 Black Friday cascade | Dev 1 | T04 | 2 h |
| [T06](T06-metric-registry.md) | Metric registry and query engine | Dev 1 | T01 (T02, T03 for all metrics) | 2 h |
| [T07](T07-stats-tools.md) | Baseline, scans, drill-down, context tools | Dev 1 | T06 | 2 h |
| [T08](T08-decompose-validate-tools.md) | Decompose, mix/rate, validation tools | Dev 1 | T06 | 1.5 h |
| [T09](T09-graph-store.md) | Graph store, bootstrap, impact weights | Dev 1 | T08 | 1.5 h |
| [T10](T10-cartographer.md) | Cartographer agent and add_metric | Dev 1 | T09, T11 | 2 h |
| [T11](T11-llm-runtime.md) | LLM agent runtime with record/replay | Dev 2 | T00 | 2 h |
| [T12](T12-incidents-reporter.md) | Incident store and Reporter | Dev 2 | T00 | 2 h |
| [T13](T13-sentinel.md) | Sentinel agent | Dev 2 | T07, T11, T12 | 1.5 h |
| [T14](T14-detective-skeptic.md) | Lead Detective and Skeptic | Dev 2 | T07, T08, T11, T15, T16 | 3 h |
| [T15](T15-data-api-a2a-client.md) | Data API and A2A client | Dev 2 | T06 | 2 h |
| [T16](T16-specialists.md) | Three A2A specialists | Dev 2 | T11, T15 | 2 h |
| [T17](T17-selleros.md) | SellerOS external agent | Dev 2 | T02, T11 | 2 h |
| [T18](T18-orchestrator-api.md) | Orchestrator, public API, SSE, dev scripts | Dev 2 | T06, T12, T13 | 2–3 h |
| [T19](T19-mcp-server.md) | MCP server | Dev 2 | T00 | 1.5 h |
| [T20](T20-chat.md) | Chat via MCP | Dev 2 | T11, T19 | 1.5 h |
| [T21](T21-ui-mocks.md) | Web UI on mock data | P3 | T00 | 3–4 h |
| [T22](T22-ui-live.md) | Web UI live wiring | P3 (+ Dev 2 review) | T18, T21 | 1.5 h |
| [T23](T23-oracle-and-eval.md) | Scenario oracle (P3) + eval harness (Dev 1) | P3 + Dev 1 | T18 | 2 h |
| [T24](T24-demo-runner-readme.md) | Demo runner script and README | Dev 2 | T18 | 1.5 h |
| [T25](T25-pitch-and-domain-notes.md) | Domain notes for prompts, slides, video (no code) | P3 | – | through the day |
| [T26](T26-deployment.md) | Docker, Caddy, tunnel, Codespaces | Dev 1 | T16–T19 | 2 h |

Card format: Goal · Read first · Files you own · Do not touch · Steps · Done when · Out of scope.
