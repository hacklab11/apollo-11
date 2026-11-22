#!/usr/bin/env bash
# Start the S8 incident on the offline demo stack and resume the clock.
# Run it right before presenting: the simulator keeps moving while Core is paused.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export COMPOSE_FILE="${COMPOSE_FILE:-$root/compose.yaml:$root/compose.e2e.yaml}"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-eye-demo}"
export COMPOSE_DISABLE_ENV_FILE=1

docker compose exec -T eye-core python - <<'PY'
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import yaml

sim = httpx.Client(base_url="http://sim:8100", timeout=30)
if any(s["id"] == "S8" for s in sim.get("/status").raise_for_status().json()["active_scenarios"]):
    sim.delete("/scenarios/s8").raise_for_status()
s8 = next(
    s
    for s in sim.post("/scenarios/s8").raise_for_status().json()
    if s["id"] == "S8" and s["active"]
)
delay = yaml.safe_load(Path("config/scenarios.yaml").read_text())["S8"]["typo_delay_hours"]
typo = datetime.fromisoformat(s8["injected_ts"]) + timedelta(hours=delay)
# The scripted model investigates S8 only from the typo hour on.
httpx.post(
    "http://scripted-model:8400/scenario", json={"investigate_after": typo.isoformat()}
).raise_for_status()
core = httpx.Client(base_url="http://web:8080", headers={"Origin": "http://web:8080"})
status = core.post("/api/clock/resume").raise_for_status().json()
gap = (typo - datetime.fromisoformat(status["sim_ts"])).total_seconds() / 3600
print(f"S8 injected at {s8['injected_ts']}; price typo at {typo.isoformat()}")
print(f"Core is at {status['sim_ts']} and running; the typo is {gap:.0f} simulated hours ahead")
PY
