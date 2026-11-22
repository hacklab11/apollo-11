# Web workspace and Core API

`packages/web` is React 19.2 + TypeScript/Vite, served by nginx on port 8080 in Compose.
Core remains private on port 8000; nginx proxies `/api/`.
The UI has three tabs: an [Overview](overview.md) home page (default), incident detail/evidence
with at most three metric charts, and a separate React Flow graph tab.
TanStack Query refetches on SSE signals with five-second polling fallback.

Core routes and SSE are in `packages/orchestrator/src/eye/api/`.
They expose status, clock control, metrics/series/graph, incidents, investigations/trail,
approvals and run control. Reads use the warehouse client. Responses—not frontend assumptions—
are authoritative. Closure requires measured recovery and a nonempty human review note;
approval/closure errors remain visible in the UI.

`npm run generate:api` runs `scripts/export_openapi.py` without starting services, then
generates `src/lib/api.generated.ts`. Commit regenerated types when API schemas change;
CI checks for drift. Do not duplicate wire types by hand.

Development commands and Vite proxy behavior: [frontend README](../../packages/web/README.md).
Agent-card/auth work is being added separately; do not infer a shared-production identity design
from the local Compose edge.
