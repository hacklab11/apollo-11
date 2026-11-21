# Eye of the System — documentation index

Hackathon track: `hackathon-rules.md`. Rules for coding agents: `../AGENTS.md`.

## Read in this order

| # | File | What it answers | Main reader |
|---|---|---|---|
| 0 | [specs/00-overview.md](specs/00-overview.md) | What we build, why, research, the hardest scenario, acceptance criteria | everyone |
| 1 | [plan/implementation-plan.md](plan/implementation-plan.md) | Who does what, when, with Devin; checkpoints; cut order; risks | everyone |
| 2 | [tasks/README.md](tasks/README.md) | Task cards T00–T26 (one Devin session each) | developers, Devin |

## Specs

| File | Topic |
|---|---|
| [specs/01-architecture.md](specs/01-architecture.md) | Processes, repo layout, tick flow, stack, Claude API rules, config, security |
| [specs/02-contracts.md](specs/02-contracts.md) | All shared data models |
| [specs/03-simulator.md](specs/03-simulator.md) | Synthetic marketplace, tables, how to generate the data |
| [specs/04-scenarios.md](specs/04-scenarios.md) | Scenario bible S1–S7, I1, I2, combined demo runs (ground truth) |
| [specs/05-metric-registry.md](specs/05-metric-registry.md) | Metrics, SQL contract, identities, causal seeds |
| [specs/06-graph-and-cartographer.md](specs/06-graph-and-cartographer.md) | Metric graph, bootstrap, automatic extension |
| [specs/07-tools.md](specs/07-tools.md) | Deterministic tools and their algorithms; data API |
| [specs/08-agent-runtime.md](specs/08-agent-runtime.md) | Agent loop on the Claude API, record/replay, budgets |
| [specs/09-core-agents.md](specs/09-core-agents.md) | Sentinel, Lead Detective, Skeptic, Reporter, stop rules, incident lifecycle |
| [specs/10-a2a-and-specialists.md](specs/10-a2a-and-specialists.md) | A2A layer and the three specialists |
| [specs/11-selleros-external.md](specs/11-selleros-external.md) | The external agent (another company) |
| [specs/12-mcp-server.md](specs/12-mcp-server.md) | MCP server for any agent |
| [specs/13-api-and-ui.md](specs/13-api-and-ui.md) | Public API, chat through MCP, web UI |
| [specs/14-testing-and-evals.md](specs/14-testing-and-evals.md) | Test levels, scenario oracle, eval harness |
| [specs/15-demo-and-pitch.md](specs/15-demo-and-pitch.md) | Demo script, slides, README |
| [specs/16-deployment.md](specs/16-deployment.md) | Free deployment: Docker + Caddy + Cloudflare tunnel; Codespaces backup |

## Rule when files disagree

Detailed spec (01–16) > overview (00) > task card. If you find a conflict, fix the spec first in a small PR, then the code.
