import type { paths, components } from "./api.generated";

export type Incident = components["schemas"]["IncidentV2"];
export type Approval = components["schemas"]["SellerRevertApproval"];
export type Metric = components["schemas"]["MetricDef"];
export type Series = components["schemas"]["Series"];
export type Status = components["schemas"]["UIStatus"];
export type Detail = components["schemas"]["IncidentDetail"];

type GetPath = {
  [P in keyof paths]: paths[P] extends { get: unknown } ? P : never;
}[keyof paths];
type GetResponse<P extends GetPath> = paths[P]["get"] extends {
  responses: { 200: { content: { "application/json": infer R } } };
}
  ? R
  : never;

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(
      typeof body?.detail === "string"
        ? body.detail
        : `Request failed (${response.status})`,
    );
  }
  return response.json() as Promise<T>;
}

export function get<P extends GetPath>(
  path: P,
  url: string = path,
): Promise<GetResponse<P>> {
  return request<GetResponse<P>>(url);
}

export function approve(id: string) {
  const body: components["schemas"]["ApprovalDecision"] = { approve: true };
  return request(`/api/approvals/${encodeURIComponent(id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function setPaused(paused: boolean) {
  return request(`/api/clock/${paused ? "pause" : "resume"}`, {
    method: "POST",
  });
}

export function closeIncident(incident: Incident, note: string) {
  const body: components["schemas"]["IncidentCloseRequest"] = {
    tenant_id: incident.tenant_id,
    note,
  };
  return request<Incident>(
    `/api/incidents/${encodeURIComponent(incident.id)}/close`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}
