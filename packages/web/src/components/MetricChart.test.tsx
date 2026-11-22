import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MetricChart } from "./MetricChart";
import type { Incident, Metric } from "../lib/api";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("includes the latest consumed hour in the half-open API window", async () => {
  const fetch = vi.fn<(url: string) => Promise<Response>>(async () =>
    Response.json({ points: [] }),
  );
  vi.stubGlobal("fetch", fetch);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MetricChart
        metric={
          { name: "gmv", dimensions: ["category"], unit: "eur" } as Metric
        }
        incident={
          {
            id: "inc-1",
            opened_ts: "2026-01-01T10:00:00Z",
            entry: { segment: { category: "electronics", country: "DE" } },
          } as unknown as Incident
        }
        end="2026-01-01T11:00:00Z"
      />
    </QueryClientProvider>,
  );
  await waitFor(() => expect(fetch).toHaveBeenCalled());
  const url = new URL(fetch.mock.calls[0][0] as string, "http://localhost");
  expect(url.searchParams.get("end")).toBe("2026-01-01T12:00:00.000Z");
  expect(url.searchParams.get("segment")).toBe('{"category":"electronics"}');
});
