# MCP

Entrypoint: `eye-mcp`; implementation: `packages/mcp/src/eye/mcp_server.py`.
The streamable HTTP server exposes `/mcp` and `/healthz`; Compose maps the first
available host port in 8110–8119 to container port 8100.
`EYE_CORE_URL` points to Core.

Tools wrap Core status, incident listing/detail, metric graph/series/explanation and metric creation.
Inputs are bounded and validated; responses include concise text plus structured data.
The MCP process does not own incidents, query simulator state, or run agent implementations.
Writes go through Core, never directly to a warehouse/store. Incident closure and seller approvals
use Core's API/UI; the MCP tool catalog does not expose those actions.

Outbound calls use the shared resilient client. Core errors become tool errors rather than
fabricated successful results. `EYE_MCP_ALLOWED_HOSTS` adds allowed hostnames for streamable
transport checks when needed by the deployment. See [web/API](web.md) for Core semantics.
