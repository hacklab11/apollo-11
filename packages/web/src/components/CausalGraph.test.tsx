import "@testing-library/jest-dom/vitest";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CausalGraph } from "./CausalGraph";

vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Background: () => null,
  Controls: () => null,
  ReactFlow: ({
    nodes,
    edges,
  }: {
    nodes: { id: string }[];
    edges: { id: string }[];
  }) => (
    <div>
      {nodes.map((n) => (
        <span key={n.id}>{n.id}</span>
      ))}
      {edges.map((e) => (
        <span key={e.id}>{e.id}</span>
      ))}
    </div>
  ),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("renders API graph node IDs and their connected edges", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json({
        nodes: [
          { id: "orders", state: "normal" },
          { id: "gmv", state: "anomaly" },
        ],
        edges: [
          {
            id: "orders-gmv",
            src: "orders",
            dst: "gmv",
            type: "formula",
            status: "accepted",
          },
        ],
      }),
    ),
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <CausalGraph />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("orders")).toBeInTheDocument();
  expect(screen.getByText("gmv")).toBeInTheDocument();
  expect(screen.getByText("orders-gmv")).toBeInTheDocument();
});
