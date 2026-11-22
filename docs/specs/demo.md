# Demo CLI

`packages/demo/src/eye_demo/cli.py` provides the standalone Typer entrypoint `demo`.
It calls HTTP only and imports no Eye application code.

```bash
uv run demo trigger s8
uv run demo status
uv run demo narrate
```

`DEMO_SIM_URL` defaults to `http://127.0.0.1:8100`; `DEMO_API_URL` defaults to
`http://127.0.0.1:8080`. CLI options can override them; use `demo --help`.
The trigger calls the simulator; it does not itself configure a provider or resume Core monitoring.

Use the UI to resume monitoring, inspect the evidence trail, review the pending seller action,
observe recovery and close with a note. A planned electronics coupon day is context, not proof that
every price/margin movement is innocent. Only S8 is implemented.

For a deterministic credential-free demonstration, use the isolated [offline Compose gate](testing.md).
The gate exercises the actual service chain with a scripted provider; it is integration evidence,
not a live-model quality score.
