import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Check, MessageSquare, ShieldCheck } from "lucide-react";
import {
  approve,
  closeIncident,
  get,
  type Approval,
  type Status,
} from "../lib/api";
import { chartMetrics, group, label, money, time } from "../lib/incidents";
import { Button } from "./ui/button";
import { MetricChart } from "./MetricChart";

export function IncidentDetail({
  id,
  status,
  approvals,
  approvalsError,
}: {
  id: string;
  status?: Status;
  approvals: Approval[];
  approvalsError: Error | null;
}) {
  const client = useQueryClient();
  const [note, setNote] = useState("");
  const detail = useQuery({
    queryKey: ["incident", id],
    queryFn: () =>
      get(
        "/api/ui/incidents/{id}",
        `/api/ui/incidents/${encodeURIComponent(id)}`,
      ),
    refetchInterval: 5000,
  });
  const metrics = useQuery({
    queryKey: ["metrics"],
    queryFn: () => get("/api/metrics"),
    refetchInterval: 30_000,
  });
  const mutation = useMutation({
    mutationFn: async (action: { approval: Approval } | { close: true }) => {
      if ("approval" in action) return approve(action.approval.id);
      if (!detail.data) throw new Error("Incident not loaded");
      return closeIncident(detail.data.incident, note.trim());
    },
    onSettled: () => {
      void client.invalidateQueries();
    },
  });
  if (detail.isPending) return <p className="empty-state">Loading incident…</p>;
  if (!detail.data)
    return (
      <p role="alert" className="empty-state">
        Could not load this incident. Retrying…
      </p>
    );
  const { incident, seller_exchange } = detail.data;
  const pending = approvals.filter(
    (a) => a.args.incident_id === id && a.status === "pending",
  );
  const approved = approvals.filter(
    (a) => a.args.incident_id === id && a.status === "approved",
  );
  const chain = [
    ...new Set(
      [
        incident.root_cause?.root_node,
        ...(incident.symptoms ?? []).map((s) => s.node),
        incident.entry.metric,
      ].filter((s): s is string => !!s),
    ),
  ];
  const ruledOut = incident.hypotheses.filter((h) => h.status === "refuted");
  const charts = chartMetrics(incident, metrics.data ?? []);
  return (
    <article className="detail-container">
      {detail.isError && (
        <p role="alert" className="error-banner">
          Refresh failed. This incident may be out of date.
        </p>
      )}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="eyebrow">
          {Object.values(incident.entry.segment).join(" / ") || "Marketplace"} ·{" "}
          {label(incident.entry.metric)}
        </p>
        <span className={`state-badge ${group(incident).toLowerCase()}`}>
          {pending.length ? "Needs you" : label(incident.status)}
        </span>
      </div>
      <h2 className="incident-title">{incident.title}</h2>
      <p className="text-muted-foreground text-sm leading-6 max-w-3xl">
        {incident.summary}
      </p>
      <div className="flex flex-wrap gap-x-6 gap-y-2 text-xs text-muted-foreground mt-4">
        <span>Opened {time(incident.opened_ts)} UTC</span>
        <span>Impact {money(incident.impact_eur_per_hour)}/hour</span>
        <span>
          {incident.verdict
            ? label(incident.verdict.toLowerCase())
            : "Gathering evidence"}
        </span>
      </div>
      <section className="cause-card" aria-label="Likely cause">
        <p className="eyebrow text-primary">Likely cause</p>
        <h3 className="text-lg font-medium leading-7 mt-2">
          {incident.root_cause?.statement ??
            "Still investigating — no cause confirmed yet."}
        </h3>
        {!!incident.root_cause?.first_change_ts && (
          <p className="text-xs text-muted-foreground mt-2">
            First change {time(incident.root_cause.first_change_ts)} UTC
          </p>
        )}
        {chain.length > 1 && (
          <>
            <h4 className="text-xs text-muted-foreground mt-5 mb-2">
              Cause and observed downstream changes
            </h4>
            <ol className="flex flex-wrap items-center gap-2">
              {chain.map((node, i) => (
                <li key={node} className="inline-flex items-center gap-2">
                  {i > 0 && (
                    <ArrowRight size={14} className="text-muted-foreground" />
                  )}
                  <span className="chain-node">{label(node)}</span>
                </li>
              ))}
            </ol>
          </>
        )}
        <div className="ruled-out">
          <ShieldCheck size={16} className="shrink-0 mt-0.5" />
          <div>
            <h4 className="font-medium text-xs">Ruled out</h4>
            {ruledOut.length ? (
              ruledOut.map((h) => (
                <p key={h.id} className="text-sm mt-1">
                  {h.statement}
                  {h.evidence_against?.length
                    ? ` — ${h.evidence_against.map((e) => e.summary).join(" ")}`
                    : ""}
                </p>
              ))
            ) : (
              <p className="text-sm mt-1 text-muted-foreground">
                No alternatives have been ruled out yet.
              </p>
            )}
          </div>
        </div>
      </section>
      <section aria-label="Measurements" className="my-7">
        <div className="section-heading">
          <h3>What the data shows</h3>
          <span>Actual vs expected</span>
        </div>
        {metrics.isError ? (
          <p role="alert" className="text-sm text-muted-foreground">
            Measurements unavailable. Retrying…
          </p>
        ) : (
          <div className="charts-grid">
            {charts.map((metric) => (
              <MetricChart
                key={metric.name}
                metric={metric}
                incident={incident}
                end={status?.sim_ts ?? incident.updated_ts}
              />
            ))}
          </div>
        )}
      </section>
      <section className="seller-card" aria-label="SellerOS exchange">
        <div className="flex items-center gap-2">
          <MessageSquare size={17} className="text-primary" />
          <h3 className="text-sm font-semibold">SellerOS exchange</h3>
          <span className="external-badge">External partner</span>
        </div>
        {seller_exchange.length ? (
          seller_exchange.map((message, i) => (
            <p key={i} className="exchange-message">
              {message.summary}
            </p>
          ))
        ) : (
          <p className="text-sm text-muted-foreground mt-3">
            No SellerOS response recorded for this incident.
          </p>
        )}
        {approved.length > 0 && incident.status !== "closed" && (
          <p className="flex items-center gap-2 text-sm text-primary mt-4">
            <Check size={16} />
            Price revert approved.
            {incident.status === "recovered"
              ? " Recovery measured."
              : " Waiting for measured recovery."}
          </p>
        )}
      </section>
      <section className="action-card" aria-label="Human actions">
        <div className="section-heading">
          <h3>Your next step</h3>
          <span>Human approval only</span>
        </div>
        {approvalsError && (
          <p role="alert" className="text-sm text-destructive mb-4">
            Could not check pending approvals. Retrying…
          </p>
        )}
        {pending.map((approval) => (
          <div key={approval.id} className="approval-row">
            <div>
              <p className="text-sm font-medium">
                Restore {approval.args.sku} to{" "}
                {money(approval.args.restore_price)}
              </p>
              <p className="text-xs text-muted-foreground mt-1">
                {approval.args.seller_id} · currently{" "}
                {money(approval.args.price)} · changed{" "}
                {time(approval.args.changed_at)} UTC
              </p>
            </div>
            <Button
              disabled={mutation.isPending || incident.status === "closed"}
              onClick={() => mutation.mutate({ approval })}
            >
              {mutation.isPending ? "Saving…" : "Approve price revert"}
            </Button>
          </div>
        ))}
        {incident.closure ? (
          <div className="text-sm">
            <p className="text-primary font-medium">
              Closed by {incident.closure.closed_by}
            </p>
            <p className="text-muted-foreground mt-1">
              {incident.closure.note}
            </p>
          </div>
        ) : (
          <div className="close-row">
            <div className="grow">
              <label htmlFor="close-note" className="text-sm font-medium">
                Close incident
              </label>
              <p
                id="close-help"
                className="text-xs text-muted-foreground mt-1 mb-3"
              >
                {incident.status === "recovered"
                  ? "Recovery measured. Add a review note to close this incident."
                  : "Available once the system measures recovery."}
              </p>
              <textarea
                id="close-note"
                aria-describedby="close-help"
                rows={2}
                placeholder="Your recovery review note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                disabled={incident.status !== "recovered" || mutation.isPending}
                className="close-input"
              />
            </div>
            <Button
              variant="outline"
              disabled={
                incident.status !== "recovered" ||
                !note.trim() ||
                mutation.isPending
              }
              onClick={() => mutation.mutate({ close: true })}
            >
              Close incident
            </Button>
          </div>
        )}
        {mutation.isError && (
          <p role="alert" className="text-sm text-destructive mt-4">
            {mutation.error.message}
          </p>
        )}
        {incident.open_questions.length > 0 && (
          <div className="text-sm text-muted-foreground mt-5">
            <h4 className="font-medium text-foreground mb-2">
              Still being investigated
            </h4>
            <ul className="list-disc pl-5 space-y-1">
              {incident.open_questions.map((q) => (
                <li key={q}>{q}</li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </article>
  );
}
