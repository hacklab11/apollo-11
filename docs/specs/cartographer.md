# Cartographer and graph refinement

`packages/agents/src/eye/agents/cartographer.py` implements the A2A `map` skill;
`service.py` exposes it over A2A. Graph storage/validation lives in `packages/data`.

Cartographer dry-runs a metric definition against available data, proposes graph placement,
then validates proposals with deterministic tools. SQL passes the read-only guard before execution.
Formula dependencies and identities supply algebraic edges; rollups, breakdowns and causal candidates
require the corresponding evidence. Rejected candidates retain reasons rather than becoming active facts.
Missing source data can leave a metric pending rather than manufacturing values.

Identity/rollup numeric tolerances and completeness rules are in [tools](tools.md).
Validation defaults to the previous 14 days, excluding supplied incident hours.
A failed formula proposal with median residual above 5% is rejected; otherwise it can remain
ambiguous. A passing one-term identity is a possible duplicate, not automatic placement.
Causal proposals accept lags 0–96h and become hypotheses when absolute correlation reaches
0.3 within ±6h of the proposed lag, or a cited formula path actually exists.
Causal proposals never become validated algebraic facts.
Causal edges require evidence beyond correlation and cannot silently create an invalid graph.
The service's cartography adapter reads the public Parquet lake into a private local DuckDB snapshot
for SQL validation; graph reads/writes go through the warehouse boundary. This is an explicit exception
to the otherwise HTTP-only agent data path, not access to simulator world state.

A breakdown self-loop means segmentation of a metric, not causal feedback.
Cycle/path helpers are structural evidence; they do not by themselves establish a causal mechanism.
