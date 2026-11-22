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
import App from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("lets a paused fresh Compose workspace resume and pause monitoring", async () => {
  let paused = true;
  const fetch = vi.fn(async (url: string) => {
    if (url === "/api/clock/resume") paused = false;
    if (url === "/api/clock/pause") paused = true;
    return Response.json(
      url.includes("status")
        ? { paused, sim_ts: null, data_complete_ts: null }
        : [],
    );
  });
  vi.stubGlobal("fetch", fetch);
  vi.stubGlobal(
    "EventSource",
    class {
      addEventListener() {}
      close() {}
    },
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <App />
    </QueryClientProvider>,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Resume monitoring" }),
  );
  const pause = await screen.findByRole("button", { name: "Pause monitoring" });
  expect(fetch).toHaveBeenCalledWith("/api/clock/resume", { method: "POST" });
  fireEvent.click(pause);
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Resume monitoring" }),
    ).toBeEnabled(),
  );
  expect(fetch).toHaveBeenCalledWith("/api/clock/pause", { method: "POST" });
});
