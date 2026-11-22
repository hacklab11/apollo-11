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
check "$(url sim 8100)/health"
check "$(url eye-specialists 8200)/healthz"
check "$(url selleros 8300)/healthz"
check "$(url eye-mcp 8100)/healthz"
check "$core/api/metrics"
check "$core/api/metrics/sessions"
check "$core/api/graph"
check "$core/"
docker compose exec -T eye-data python - <<'PY'
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8150/healthz", timeout=10) as response:
    assert response.status == 200
    print("OK eye-data /healthz")
PY
docker compose exec -T eye-core python - <<'PY'
import asyncio
import os
import urllib.request

from mcp import Client

with urllib.request.urlopen("http://eye-data:8150/v1/metrics", timeout=10) as response:
    assert response.status == 200
    print("OK Core -> eye-data /v1/metrics")


async def main():
    async with Client(os.environ["EYE_MCP_URL"]) as client:
        tools = (await client.list_tools()).tools
        assert "system_status" in {tool.name for tool in tools}, tools
        print(f"OK Core -> MCP tools/list ({len(tools)} tools)")


asyncio.run(main())
PY
printf 'Compose smoke passed (health, Core warehouse reads, web UI).\n'
