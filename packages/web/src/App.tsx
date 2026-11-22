import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, CheckCheck, GitBranch, Inbox } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./components/ui/tabs";
import { CausalGraph } from "./components/CausalGraph";
import { IncidentDetail } from "./components/IncidentDetail";
import { get } from "./lib/api";
import { group, time } from "./lib/incidents";

export default function App() {
  const client = useQueryClient();
  const [selected, setSelected] = useState<string>();
  const [streamLive, setStreamLive] = useState(false);
  const status = useQuery({
    queryKey: ["status"],
    queryFn: () => get("/api/ui/status"),
    refetchInterval: 5000,
  });
  const incidents = useQuery({
    queryKey: ["incidents"],
    queryFn: () => get("/api/ui/incidents"),
    refetchInterval: 5000,
  });
  const approvals = useQuery({
    queryKey: ["approvals"],
    queryFn: () => get("/api/approvals/seller-revert"),
    refetchInterval: 5000,
  });
  const items = incidents.data ?? [];
  const selectedId = items.some((item) => item.id === selected)
    ? selected
    : (items.find((item) => group(item) === "Open")?.id ?? items[0]?.id);
  const pending = new Set(
    approvals.data
      ?.filter((item) => item.status === "pending")
      .map((item) => item.args.incident_id),
  );

  useEffect(() => {
    const events = new EventSource("/api/events");
    let timer: ReturnType<typeof setTimeout> | undefined;
    const invalidate = () => {
      if (timer) return;
      timer = setTimeout(() => {
        timer = undefined;
        void client.invalidateQueries();
      }, 500);
    };
    // Events never carry UI state: reconnect and polling use the same reads.
    for (const event of [
      "tick",
      "incident_upserted",
      "trail_step",
      "graph_changed",
    ])
      events.addEventListener(event, invalidate);
    events.onopen = () => {
      setStreamLive(true);
      invalidate();
    };
    events.onerror = () => setStreamLive(false);
    return () => {
      events.close();
      clearTimeout(timer);
    };
  }, [client]);

  return (
    <Tabs defaultValue="incidents" className="min-h-screen">
      <header className="app-header">
        <div className="flex items-center gap-3">
          <span className="brand-icon">
            <Activity size={21} />
          </span>
          <div>
            <h1 className="text-base font-semibold tracking-tight">
              Eye of the System
            </h1>
            <p className="text-xs text-muted-foreground">
              Marketplace observatory
            </p>
          </div>
        </div>
        <div className="clock" aria-live="polite">
          {status.data ? (
            <>
              <span>
                Sim <strong>{time(status.data.sim_ts)}</strong>
              </span>
              <span className="text-border">/</span>
              <span>
                Data complete to{" "}
                <strong>{time(status.data.data_complete_ts)}</strong>
              </span>
              <span className="text-xs">UTC</span>
            </>
          ) : incidents.isError ? (
            <p role="alert" className="empty-state">
              Could not load incidents. Retrying automatically…
            </p>
          ) : incidents.isPending ? (
            <p className="empty-state">Loading incidents…</p>
          ) : (
            "Connecting to the system…"
          )}
        </div>
        <TabsList aria-label="Workspace">
          <TabsTrigger value="incidents">
            <Inbox size={15} />
            Incidents
          </TabsTrigger>
          <TabsTrigger value="graph">
            <GitBranch size={15} />
            Graph
          </TabsTrigger>
        </TabsList>
      </header>
      {(status.isError || incidents.isError) && (
        <p role="alert" className="error-banner">
          Core not reachable. Showing the last available data; retrying
          automatically.
        </p>
      )}
      <TabsContent value="incidents" className="workspace">
        <aside aria-label="Incidents" className="incident-sidebar">
          <div className="sidebar-heading">
            <h2 className="font-semibold">Incidents</h2>
            <span className="text-xs text-muted-foreground">
              {streamLive ? "Live updates" : "Polling · 5s"}
            </span>
          </div>
          {incidents.isPending && (
            <p className="p-5 text-sm text-muted-foreground">
              Loading incidents…
            </p>
          )}
          {(["Open", "Recovered", "Closed"] as const).map((section) => {
            const rows = items.filter((item) => group(item) === section);
            return (
              <section
                key={section}
                className="incident-group"
                aria-label={section}
              >
                <h3 className="group-heading">
                  {section}
                  <span>{rows.length}</span>
                </h3>
                {!rows.length && (
                  <p className="px-5 pb-4 text-xs text-muted-foreground">
                    No {section.toLowerCase()} incidents.
                  </p>
                )}
                {rows.map((item) => (
                  <button
                    key={item.id}
                    onClick={() => setSelected(item.id)}
                    aria-current={selectedId === item.id ? "true" : undefined}
                    className="incident-row"
                  >
                    <span className="flex items-center justify-between gap-2">
                      <span className={`status-dot ${section.toLowerCase()}`} />
                      <span className="text-xs text-muted-foreground">
                        {time(item.opened_ts)}
                      </span>
                    </span>
                    <span className="block font-medium text-sm leading-5 mt-2">
                      {item.title}
                    </span>
                    <span className="block text-xs text-muted-foreground mt-2">
                      {pending.has(item.id) ? (
                        <span className="needs-you">
                          Needs you · price revert
                        </span>
                      ) : item.status === "recovered" ? (
                        "Recovery measured · ready to close"
                      ) : item.status === "closed" ? (
                        "Reviewed and closed"
                      ) : (
                        "Investigation in progress"
                      )}
                    </span>
                  </button>
                ))}
              </section>
            );
          })}
          <p className="px-5 py-6 text-xs text-muted-foreground leading-5">
            Recovery is measured by the system.
            <br />
            Closing an incident is always your call.
          </p>
        </aside>
        <main className="incident-main">
          {selectedId ? (
            <IncidentDetail
              key={selectedId}
              id={selectedId}
              status={status.data}
              approvals={approvals.data ?? []}
              approvalsError={approvals.error}
            />
          ) : (
            <div className="empty-state">
              <CheckCheck size={32} className="mx-auto mb-4 text-primary" />
              <h2 className="font-semibold text-lg">
                Nothing needs your attention
              </h2>
              <p className="mt-2 text-sm">
                The system is watching. Incidents will appear here when it finds
                a change worth investigating.
              </p>
            </div>
          )}
        </main>
      </TabsContent>
      <TabsContent value="graph" className="p-6">
        <CausalGraph />
      </TabsContent>
    </Tabs>
  );
}
