#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export COMPOSE_FILE="${COMPOSE_FILE:-$root/compose.yaml:$root/compose.e2e.yaml}"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-eye-s8-e2e}"
export COMPOSE_DISABLE_ENV_FILE=1
trap 'docker compose logs --no-color --tail=200 >&2' ERR

mapfile -t detectives < <(docker compose ps -q detective)
test "${#detectives[@]}" -eq 3
printf 'OK Detective replicas: 3\n'
docker compose exec -T sentinel python - <<'PY'
import httpx

response = httpx.get("http://example.com", timeout=5)
assert response.status_code == 403, response.status_code
print("OK proxy refused non-allowlisted host (403)")
try:
    httpx.get("http://1.1.1.1", trust_env=False, timeout=3)
except httpx.TransportError:
    print("OK agent direct external request blocked")
else:
    raise AssertionError("Agent has a direct internet route")
try:
    httpx.get("http://scripted-model:8400/healthz", trust_env=False, timeout=3)
except httpx.TransportError:
    print("OK scripted-model inaccessible directly from agent")
else:
    raise AssertionError("Scripted model bypassed proxy")
assert httpx.get("http://scripted-model:8400/healthz", timeout=5).status_code == 200
print("OK agent reaches scripted-model through proxy")
PY
docker compose exec -T eye-core python /e2e/run_s8.py
docker compose exec -T eye-core python /e2e/queued.py
