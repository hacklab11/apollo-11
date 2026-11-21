# T26 Docker, Caddy, tunnel, Codespaces

Owner: Dev 1 · Depends on: T16, T17, T18, T19 · Blocks: demo on a public URL

## Goal
One image that runs the whole platform with one public, password-protected URL, on the demo laptop and in Codespaces.

## Read first
`docs/specs/16-deployment.md` (all).

## Files you own
`Dockerfile`, `docker-compose.yml`, `Caddyfile`, `scripts/run_all.sh`, `scripts/tunnel.sh`, `.env.example`, `.devcontainer/devcontainer.json`, `.dockerignore`.

## Steps
1. Image: `python:3.13-slim`, install `uv`, `caddy` (static binary), `cloudflared` not inside the image (runs on the host). `uv sync --frozen` for both projects.
2. `run_all.sh`: start core, specialists, selleros, mcp, caddy; exit when any process exits (`wait -n`); forward signals.
3. Caddy config from 16 §3 with basic auth from env; verify SSE and MCP through the proxy.
4. Compose: one service, port 8080, `env_file: .env`, volume `./data:/app/data` and `./external/selleros/data:/app/external/selleros/data`.
5. Devcontainer for Codespaces: Docker-in-Docker feature, forward 8080.
6. `.dockerignore`: `.env`, `data/`, `.venv/`, `.git/`.

## Done when
16 §7 items 1–5, each with the command and its output in the PR.

## Out of scope
Paid hosting.
