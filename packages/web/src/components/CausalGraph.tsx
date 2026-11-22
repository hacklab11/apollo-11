import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import dagre from "@dagrejs/dagre";
import { Background, Controls, ReactFlow, MarkerType } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { get } from "../lib/api";
import { label } from "../lib/incidents";

export function CausalGraph() {
  const query = useQuery({
    queryKey: ["graph"],
    queryFn: () => get("/api/graph"),
    refetchInterval: 15_000,
  });
  const graph = useMemo(() => {
    const layout = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
    layout.setGraph({ rankdir: "LR", ranksep: 100, nodesep: 30 });
    const nodes = (query.data?.nodes ?? []).filter(
      (node) => typeof node.id === "string",
    );
    const names = new Set(nodes.map((node) => String(node.id)));
    nodes.forEach((node) =>
      layout.setNode(String(node.id), { width: 190, height: 65 }),
    );
    const edges = (query.data?.edges ?? []).filter(
      (edge) => names.has(edge.src) && names.has(edge.dst),
    );
    edges.forEach((edge) => layout.setEdge(edge.src, edge.dst));
    dagre.layout(layout);
    return {
      nodes: nodes.map((node) => {
        const id = String(node.id),
          position = layout.node(id);
        return {
          id,
          position: { x: position.x - 95, y: position.y - 32.5 },
          data: { label: label(id) },
          style: {
            width: 190,
            borderColor:
              node.state === "incident" || node.state === "anomaly"
                ? "#b45309"
                : "#cbd5e1",
            borderRadius: 8,
          },
        };
      }),
      edges: edges.map((edge) => ({
        id: edge.id,
        source: edge.src,
        target: edge.dst,
        label: edge.type === "formula" ? "=" : edge.status,
        markerEnd: { type: MarkerType.ArrowClosed },
        style: {
          stroke: edge.status === "rejected" ? "#cbd5e1" : "#64748b",
          strokeDasharray: edge.type === "formula" ? undefined : "5 5",
          opacity: edge.status === "rejected" ? 0.5 : 1,
        },
      })),
    };
  }, [query.data]);
  if (query.isPending)
    return <p className="empty-state">Loading causal graph…</p>;
  if (query.isError)
    return (
      <p role="alert" className="empty-state">
        Could not load the graph. Retrying…
      </p>
    );
  return (
    <section aria-label="Full causal graph" className="graph-panel">
      <div className="p-5 border-b">
        <h2 className="font-semibold">The full picture</h2>
        <p className="text-sm text-muted-foreground mt-1">
          Solid: formula · dashed: causal hypothesis or breakdown · faded:
          rejected. Scroll to zoom; drag to explore.
        </p>
      </div>
      <div className="h-[72vh]">
        <ReactFlow
          nodes={graph.nodes}
          edges={graph.edges}
          fitView
          minZoom={0.05}
          nodesDraggable={false}
          nodesConnectable={false}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
    </section>
  );
}
