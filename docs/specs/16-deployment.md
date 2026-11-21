# 16 Deployment

Goal: the whole system (simulator + core agents + 3 specialists + SellerOS + MCP + UI) runs side by side on one machine and has one public HTTPS URL for the judges. Cost: zero, or close to zero, for the hackathon.
Task: T26.

## 1. Constraints that drive the choice

- The simulator and the orchestrator are a long-running loop. Platforms that sleep when idle or scale to zero (Render free, Cloud Run with min instances 0, Vercel) stop the loop. They do not fit.
- DuckDB needs a local disk that survives while the demo runs.
- 4 processes. Only 2 need to be public: the UI/API (core) and MCP. SellerOS is public too, for the story "an external agent at another address".
- `ANTHROPIC_API_KEY` must stay secret. A public chat box spends API money, so the public URL needs a password.
- 24 hours of time. No account approval queues.

## 2. Recommendation

**Primary: one Docker image, run with Docker Compose. Public URL through Cloudflare Quick Tunnel.**

| Where the container runs | Cost | Why |
|---|---|---|
| A team laptop (demo machine) | free | no sleep, full control, fastest; the tunnel gives a public HTTPS URL in seconds with `cloudflared tunnel --url http://localhost:8080`, no account needed |
| GitHub Codespaces (backup, same compose file) | free monthly quota for personal accounts (check current hours) | a cloud copy the whole team can open; port 8080 can be forwarded as public; keep it running during judging |

Why this: it is the only setup that is free, has no sleep, needs no sign-up review, and is the same on every machine. The Docker image also proves "deployable platform" in the pitch.

**Options we do not recommend for this hackathon:**

| Option | Problem |
|---|---|
| Hugging Face Docker Space (cpu-basic) | Docs list 2 vCPU / 16 GB free with sleep after 48 h. Community reports from mid-2026 say free cpu-basic now hits quota limits or needs PRO. Check your account before you rely on it. Only one public port (use the Caddy routing below). |
| Oracle Cloud Always Free VM | Good always-on VM, but sign-up and card checks can take hours or fail. Use only if a team member already has an account. |
| Render free, Cloud Run, Vercel | sleep / scale to zero → the simulator stops |
| Fly.io, Railway | trial credits only; fine if you already have credits |

## 3. Container layout

One image, `supervisord` (or a small `scripts/run_all.sh` with `wait -n`) starts 5 processes:

```
:8000 core (FastAPI: UI, /api, /data, SSE)
:8100 mcp  (/mcp)
:8200 specialists (/growth, /marketplace, /discovery A2A)
:8300 selleros (A2A)
:8080 caddy (the only published port)
```

`Caddyfile`:

```
:8080 {
    basicauth /* {
        {$DEMO_USER} {$DEMO_PASSWORD_HASH}
    }
    handle_path /mcp* {
        reverse_proxy localhost:8100
    }
    handle_path /selleros/* {
        reverse_proxy localhost:8300
    }
    handle {
        reverse_proxy localhost:8000
    }
}
```

- Specialists stay internal. Core calls them on `localhost:8200`. SellerOS's public URL is for show; core calls it on `localhost:8300`.
- SSE through Caddy works by default (it flushes streamed responses). Verify in T26.
- MCP clients outside (Claude Desktop) connect to `https://<tunnel>/mcp` with the basic-auth header. If a client cannot send headers, use a token in the path: `/mcp/<token>`, checked by Caddy. Decide in T26 after reading the MCP client's options.

## 4. Files (T26)

```
Dockerfile              # python:3.13-slim, uv, caddy binary, copy repo, uv sync --frozen
docker-compose.yml      # one service "eye", port 8080, env_file .env, volume ./data:/app/data
Caddyfile
scripts/run_all.sh      # starts the 4 python processes + caddy; exits if one dies
scripts/tunnel.sh       # cloudflared tunnel --url http://localhost:8080 ; prints the URL
.env.example            # ANTHROPIC_API_KEY=, DEMO_USER=, DEMO_PASSWORD_HASH=, LLM_MODE=
.devcontainer/devcontainer.json   # Codespaces: Docker-in-Docker, forwards 8080
```

`.env` is gitignored. Generate the password hash with `caddy hash-password`.

## 5. Steps to deploy (for the team)

1. `cp .env.example .env` and fill in the key and the demo password hash.
2. `uv run eye-sim generate --days 28 --seed 42` (optional: pre-build the data so startup is fast).
3. `docker compose up --build -d`
4. `./scripts/tunnel.sh` → copy the `https://*.trycloudflare.com` URL.
5. Open the URL, log in, check `/api/status`: all agents green.
6. For the backup in Codespaces: open the repo in a codespace, repeat steps 1–4 there (or forward port 8080 as public instead of the tunnel).

## 6. Demo-day safety

- `LLM_MODE=replay` with a recorded `data/llm_cache.jsonl` from a rehearsal: the demo works even if the API is slow. Switch to `live` to show it is real.
- Quick tunnels get a new URL at each start. Start the tunnel once before judging and do not restart it.
- Keep the laptop on power, with sleep disabled.
- The backup video (`15-demo-and-pitch.md`) covers a network failure.

## 7. Acceptance (T26)

1. `docker compose up --build` on a clean machine → `GET /api/status` through port 8080 returns all 4 components healthy within 90 s.
2. Without the password: 401. With it: UI loads, SSE events arrive.
3. Claude Code (or Claude Desktop) connects to `https://<tunnel>/mcp` and lists the MCP tools.
4. `docker compose restart` keeps `data/` (volume) and resumes the clock.
5. No secret appears in the image (`docker history`, image layers) or in logs.
