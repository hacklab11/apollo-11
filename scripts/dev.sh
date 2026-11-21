#!/usr/bin/env bash
set -euo pipefail
set -m
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

pids=()
stop_all() {
  trap - EXIT INT TERM
  for pid in "${pids[@]}"; do
    kill -TERM -- "-$pid" 2>/dev/null || true
  done
  wait || true
}
trap stop_all EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start() {
  local name=$1
  shift
  "$@" > >(sed -u "s/^/[$name] /") 2>&1 &
  pids+=("$!")
}

start core uv run eye-core
start specialists uv run eye-specialists
start selleros uv run --project external/selleros selleros
start mcp uv run eye-mcp
wait -n "${pids[@]}"
