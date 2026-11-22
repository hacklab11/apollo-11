#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

# Discover mapped ports so .env overrides and Compose port allocation both work.
url() {
  printf 'http://%s' "$(docker compose port "$1" "$2" | head -n 1)"
}
check() {
  curl --fail --silent --show-error --max-time 10 "$1" > /dev/null
  printf 'OK %s\n' "$1"
}

core=$(url eye-core 8000)
check "$core/api/status"
check "$(url eye-specialists 8200)/healthz"
check "$(url selleros 8300)/healthz"
check "$(url eye-mcp 8100)/healthz"
check "$core/api/metrics"
check "$core/"
printf 'Compose smoke passed (health, public metrics API, web UI).\n'
