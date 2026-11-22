#!/usr/bin/env bash
set -euo pipefail
set -m
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

pids=()
starting=0
exit_code=0
stop_all() {
  trap - EXIT
  trap '' INT TERM
  local pid
  for pid in "${pids[@]}"; do
    kill -TERM -- "-$pid" 2>/dev/null || true
  done
  wait || true
}
on_signal() {
  if (( starting )); then
    exit_code=$1
  else
    exit "$1"
  fi
}
trap stop_all EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

start() {
  local name=$1
  shift
  # Defer shutdown until the new process group is tracked.
  starting=1
  "$@" > >(sed -u "s/^/[$name] /") 2>&1 &
  pids+=("$!")
  starting=0
  (( exit_code == 0 )) || exit "$exit_code"
}

start core uv run eye-core
start specialists uv run eye-specialists
start selleros uv run --project packages/selleros selleros
start mcp uv run eye-mcp
wait -n "${pids[@]}"
