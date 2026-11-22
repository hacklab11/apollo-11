import "@testing-library/jest-dom/vitest";
import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { IncidentDetail } from "./IncidentDetail";
import type { Approval, Incident } from "../lib/api";
import { chartMetrics, group } from "../lib/incidents";

vi.mock("./MetricChart", () => ({
  MetricChart: () => <div>Metric chart</div>,
}));

const incident: Incident = {
  id: "inc-1",
  context_id: "inc-1",
  fingerprint: "gmv-electronics",
  verdict: "CONFIRMED",
  last_seen_ts: "2026-01-01T11:00:00Z",
  occurrences: 1,
  impact_eur_total: 200,
  affected_actors: [],
  contributions: [],
  root_cause: null,
  tenant_id: "default",
  status: "open",
  title: "Price change",
  summary: "Manual price drop",
  opened_ts: "2026-01-01T10:00:00Z",
  updated_ts: "2026-01-01T11:00:00Z",
  impact_eur_per_hour: 200,
  entry: {
    id: "anomaly-1",
    kind: "point",
    z: -4,
    completeness: 1,
    priority: 1,
    metric: "gmv",
    segment: { category: "electronics" },
    ts: "2026-01-01T10:00:00Z",
    window_hours: 1,
    actual: 100,
    expected: 300,
    delta: -200,
    delta_pct: -0.67,
    impact_eur_per_hour: 200,
  },
  hypotheses: [],
  symptoms: [],
  open_questions: [],
  suggested_actions: [],
  investigation_ids: [],
  notifications: [],
};
const approval = {
  id: "approval-1",
  tool_name: "revert_manual_price",
  status: "pending",
  decided_by: null,
  args: {
    incident_id: incident.id,
    seller_id: "S-24",
    sku: "E01",
    price: 9,
    restore_price: 89,
    changed_at: incident.entry.ts,
  },
} as Approval;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function show(status: Incident["status"], failed = false) {
  const fetch = vi.fn(async (url: string) => {
    if (url === "/api/metrics") return Response.json([]);
    if (url.startsWith("/api/ui/incidents/"))
      return Response.json({
        incident: { ...incident, status },
        seller_exchange: [],
      });
    return Response.json(
      failed
        ? { detail: "Recovery changed; refresh and retry" }
        : { ...incident, status: "closed" },
      { status: failed ? 409 : 200 },
    );
  });
  vi.stubGlobal("fetch", fetch);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <IncidentDetail
        id={incident.id}
        approvals={[approval]}
        approvalsError={null}
      />
    </QueryClientProvider>,
  );
  return fetch;
}

it("requires measured recovery and sends no browser-supplied identity on approval", async () => {
  const fetch = show("open");
  const approve = await screen.findByRole("button", {
    name: "Approve price revert",
  });
  expect(screen.getByRole("button", { name: "Close incident" })).toBeDisabled();
  expect(screen.getByLabelText("Close incident")).toBeDisabled();
  fireEvent.click(approve);
  await waitFor(() =>
    expect(fetch).toHaveBeenCalledWith("/api/approvals/approval-1", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: '{"approve":true}',
    }),
  );
});

it("requires a review note and surfaces a rejected close without hiding recovery", async () => {
  const fetch = show("recovered", true);
  const close = await screen.findByRole("button", { name: "Close incident" });
  expect(close).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Close incident"), {
    target: { value: "Reviewed recovery" },
  });
  expect(close).toBeEnabled();
  fireEvent.click(close);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Recovery changed; refresh and retry",
  );
  expect(fetch).toHaveBeenCalledWith("/api/incidents/inc-1/close", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: '{"tenant_id":"default","note":"Reviewed recovery"}',
  });
});

it("separates recovered and closed states and caps distinct charts at three", () => {
  expect(group({ ...incident, status: "recovered" })).toBe("Recovered");
  expect(group({ ...incident, status: "closed" })).toBe("Closed");
  const symptoms = ["price", "orders", "stock", "gmv"].map((node) => ({
    node,
  }));
  const metrics = ["gmv", "price", "orders", "stock"].map((name) => ({ name }));
  expect(
    chartMetrics(
      { ...incident, symptoms } as Incident,
      metrics as Parameters<typeof chartMetrics>[1],
    ).map((m) => m.name),
  ).toEqual(["gmv", "price", "orders"]);
});
