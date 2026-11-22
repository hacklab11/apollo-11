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
import type { Incident, Status } from "../lib/api";
import { Overview } from "./Overview";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const status = {
  paused: true,
  sim_ts: "2026-12-04T10:00:00Z",
  data_complete_ts: "2026-12-04T09:00:00Z",
} as Status;

function incident(
  id: string,
  metric: string,
  impact: number,
  state = "open",
): Incident {
  return {
    id,
    title: `${metric}: HYPOTHESIS`,
    status: state,
    impact_eur_per_hour: impact,
    entry: { metric, segment: {} },
  } as unknown as Incident;
}

function renderOverview(incidents: Incident[], onOpen = vi.fn()) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/metrics")
        return Response.json([{ name: "gmv", unit: "eur" }]);
      if (url.startsWith("/api/metrics/orders?"))
        return Response.json({ detail: "boom" }, { status: 500 });
      return Response.json({
        metric: "x",
        segment: {},
        points: [
          { ts: "2026-12-04T08:00:00Z", value: 90, expected: 100 },
          { ts: "2026-12-04T09:00:00Z", value: 110, expected: 100 },
        ],
      });
    }),
  );
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <Overview status={status} incidents={incidents} onOpenIncident={onOpen} />
    </QueryClientProvider>,
  );
  return onOpen;
}

it("sums open incidents and their hourly impact", () => {
  renderOverview([
    incident("a", "gmv", 100),
    incident("b", "aov", 50, "persisting"),
    incident("c", "orders", 999, "recovered"),
  ]);
  const strip = screen.getByRole("region", { name: "Status" });
  expect(strip).toHaveTextContent("Open incidents2");
  expect(strip).toHaveTextContent("Impact now€150/h");
  expect(strip).toHaveTextContent("MonitoringPaused");
});

it("shows the latest value against expected and opens the matching incident", async () => {
  const onOpen = renderOverview([incident("a", "gmv", 100)]);
  const tile = screen.getByRole("region", { name: "gmv tile" });
  expect(await screen.findAllByText("+10.0% vs expected")).not.toHaveLength(0);
  expect(tile).toHaveTextContent("€110");
  fireEvent.click(
    screen.getByRole("button", { name: "Open incident: gmv: HYPOTHESIS" }),
  );
  expect(onOpen).toHaveBeenCalledWith("a");
});

it("keeps other tiles when one metric request fails", async () => {
  renderOverview([]);
  expect(await screen.findByText("Unavailable. Retrying…")).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "orders tile" })).toHaveTextContent(
    "Unavailable",
  );
  await waitFor(() =>
    expect(screen.getByRole("region", { name: "aov tile" })).toHaveTextContent(
      "vs expected",
    ),
  );
});
