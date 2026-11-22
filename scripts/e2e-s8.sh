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
docker compose exec -T eye-core python /e2e/run_s8.py
