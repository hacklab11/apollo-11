import { useQuery } from "@tanstack/react-query";
import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import {
  Area,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { get, type Incident, type Metric, type Status } from "../lib/api";
import { group, label, money, time } from "../lib/incidents";

// shortcut: fixed list (registry has no "key metric" flag); move to config/ when users need to change it.
const KEY_METRICS = [
  "gmv",
  "orders",
  "aov",
  "checkout_conversion",
  "payment_success_rate",
  "contribution_margin",
  "sessions",
  "ad_revenue",
];
const HOUR = 3600_000;

function formatValue(value: number | null | undefined, unit?: string) {
  if (value == null) return "missing";
  if (unit === "eur") return money(value);
  if (unit === "ratio")
    return `${(value * 100).toLocaleString("en", { maximumFractionDigits: 2 })}%`;
  return Intl.NumberFormat("en", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

export function Overview({
  status,
  incidents,
  onOpenIncident,
}: {
  status: Status | undefined;
  incidents: Incident[];
  onOpenIncident: (id?: string) => void;
}) {
  const metrics = useQuery({
    queryKey: ["metrics"],
    queryFn: () => get("/api/metrics"),
  });
  const open = incidents.filter((item) => group(item) === "Open");
  const impact = open.reduce(
    (sum, item) => sum + (item.impact_eur_per_hour ?? 0),
    0,
  );
  const lagHours =
    status?.sim_ts && status.data_complete_ts
      ? Math.max(
          0,
          Math.round(
            (Date.parse(status.sim_ts) - Date.parse(status.data_complete_ts)) /
              HOUR,
          ),
        )
      : undefined;

  return (
    <div className="detail-container">
      <p className="eyebrow">Overview</p>
      <h2 className="incident-title">Marketplace health</h2>
      <section aria-label="Status" className="overview-strip">
        <div className="stat-card">
          <span className="eyebrow">Open incidents</span>
          {open.length ? (
            <button
              type="button"
              className="stat-value stat-link"
              onClick={() => onOpenIncident(open[0]?.id)}
            >
              {open.length}
            </button>
          ) : (
            <span className="stat-value">0</span>
          )}
        </div>
        <div className="stat-card">
          <span className="eyebrow">Impact now</span>
          <span className={`stat-value ${impact ? "text-destructive" : ""}`}>
            {money(impact)}/h
          </span>
        </div>
        <div className="stat-card">
          <span className="eyebrow">Monitoring</span>
          <span className="stat-value">
            {!status ? "Unknown" : status.paused ? "Paused" : "Running"}
          </span>
        </div>
        <div className="stat-card">
          <span className="eyebrow">Data freshness</span>
          <span className="stat-value">
            {lagHours === undefined
              ? "Unknown"
              : lagHours === 0
                ? "Up to date"
                : `${lagHours} h behind`}
          </span>
        </div>
      </section>
      <div className="section-heading mt-8">
        <h3>Key metrics</h3>
        <span>Last 48 hours · whole marketplace · UTC</span>
      </div>
      <div className="overview-grid">
        {KEY_METRICS.map((name) => (
          <MetricTile
            key={name}
            name={name}
            metric={metrics.data?.find((m) => m.name === name)}
            end={status?.data_complete_ts ?? undefined}
            incident={open.find((item) => item.entry.metric === name)}
            onOpenIncident={onOpenIncident}
          />
        ))}
      </div>
    </div>
  );
}

function MetricTile({
  name,
  metric,
  end,
  incident,
  onOpenIncident,
}: {
  name: string;
  metric: Metric | undefined;
  end: string | undefined;
  incident: Incident | undefined;
  onOpenIncident: (id?: string) => void;
}) {
  // API windows are half-open; include the latest processed hourly measurement.
  const until = end ? Date.parse(end) + HOUR : undefined;
  const params = until
    ? new URLSearchParams({
        start: new Date(until - 48 * HOUR).toISOString(),
        end: new Date(until).toISOString(),
        segment: "{}",
      }).toString()
    : "";
  const query = useQuery({
    queryKey: ["overview-metric", name, params],
    queryFn: () =>
      get(
        "/api/metrics/{name}",
        `/api/metrics/${encodeURIComponent(name)}?${params}`,
      ),
    enabled: !!until,
    refetchInterval: 10_000,
  });
  const points = (query.data?.points ?? []).map((p) => ({
    ...p,
    band: p.lower != null && p.upper != null ? [p.lower, p.upper] : undefined,
  }));
  const latest = [...points].reverse().find((p) => p.value != null);
  const change =
    latest?.value != null && latest.expected
      ? (latest.value - latest.expected) / Math.abs(latest.expected)
      : undefined;
  const title = label(name);

  return (
    <section className="chart-card" aria-label={`${title} tile`}>
      <div className="flex items-start justify-between gap-2">
        <h4 className="text-sm font-medium capitalize">{title}</h4>
        <span className="text-xs text-muted-foreground">
          {metric?.unit === "eur" ? "EUR" : metric?.unit}
        </span>
      </div>
      {query.isError ? (
        <p role="alert" className="tile-message">
          Unavailable. Retrying…
        </p>
      ) : query.isPending ? (
        <p className="tile-message">Loading…</p>
      ) : !points.length ? (
        <p className="tile-message">No measurements in this window.</p>
      ) : (
        <>
          <p className="mt-2 flex items-baseline gap-2">
            <span className="text-2xl font-semibold tracking-tight">
              {formatValue(latest?.value, metric?.unit)}
            </span>
            {change !== undefined && (
              <span
                className={`flex items-center text-xs font-medium ${
                  Math.abs(change) >= 0.05
                    ? "text-destructive"
                    : "text-muted-foreground"
                }`}
              >
                {change >= 0 ? (
                  <ArrowUpRight size={13} aria-hidden="true" />
                ) : (
                  <ArrowDownRight size={13} aria-hidden="true" />
                )}
                {`${change >= 0 ? "+" : ""}${(change * 100).toFixed(1)}% vs expected`}
              </span>
            )}
          </p>
          <div className="h-20 mt-3" aria-hidden="true">
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <ComposedChart
                data={points}
                margin={{ top: 2, right: 2, bottom: 0, left: 2 }}
              >
                <XAxis dataKey="ts" hide />
                <YAxis hide domain={["auto", "auto"]} />
                <Tooltip
                  labelFormatter={(value) => `${time(String(value))} UTC`}
                />
                <Area
                  name="Expected range"
                  dataKey="band"
                  stroke="none"
                  fill="#176b67"
                  fillOpacity={0.08}
                  isAnimationActive={false}
                />
                <Line
                  name="Expected"
                  dataKey="expected"
                  type="monotone"
                  stroke="#94a3b8"
                  strokeDasharray="4 4"
                  dot={false}
                  isAnimationActive={false}
                />
                <Line
                  name="Actual"
                  dataKey="value"
                  type="monotone"
                  stroke="#176b67"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
      {incident && (
        <button
          type="button"
          className="tile-incident"
          onClick={() => onOpenIncident(incident.id)}
        >
          <span className="status-dot open" aria-hidden="true" />
          Open incident: {incident.title}
        </button>
      )}
    </section>
  );
}
