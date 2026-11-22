# Incident workspace

React + Vite + TypeScript, served by nginx at http://localhost:8080 in Compose.
See the root README for live and credential-free S8 commands.

## Develop

Use Node 22 or newer. From this directory:

```sh
npm ci
npm run generate:api
npm run dev
```

Vite binds to loopback and proxies `/api` to the running Compose edge at port
8080. Core stays private; nginx supplies the trusted local operator identity.
Do not send human identity or service tokens from frontend code.

```sh
npm run lint
npm test
npm run build
```

`generate:api` exports FastAPI OpenAPI without starting services, then runs
`openapi-typescript`. Commit the generated types when the API changes; CI checks
for drift. API responses remain authoritative: TanStack Query refetches on SSE
signals and polls every five seconds if streaming is unavailable.

The incident view shows at most three metric charts. The React Flow graph lives
on its own tab. Approval and close errors remain visible, and close requires a
measured recovery and a review note; backend rules enforce both independently.
