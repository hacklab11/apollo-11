import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { get, type Incident, type Metric } from "../lib/api";
import { label, time } from "../lib/incidents";

export function MetricChart({
  metric,
  incident,
  end,
}: {
  metric: Metric;
  incident: Incident;
  end: string;
}) {
  const segment = Object.fromEntries(
    Object.entries(incident.entry.segment).filter(([key]) =>
      metric.dimensions.includes(key as never),
    ),
  );
  const params = new URLSearchParams({
    // API windows are half-open; include the latest processed hourly measurement.
    end: new Date(new Date(end).getTime() + 3600_000).toISOString(),
    start: new Date(
      new Date(incident.opened_ts).getTime() - 24 * 3600_000,
    ).toISOString(),
    segment: JSON.stringify(segment),
  });
  const query = useQuery({
    queryKey: ["metric", incident.id, metric.name, params.toString()],
    queryFn: () =>
      get(
        "/api/metrics/{name}",
        `/api/metrics/${encodeURIComponent(metric.name)}?${params}`,
      ),
    refetchInterval: 10_000,
  });
  const points = query.data?.points ?? [];
  const latest = points.at(-1);
  return (
    <section className="chart-card" aria-label={`${label(metric.name)} chart`}>
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-medium capitalize">{label(metric.name)}</h3>
        <span className="text-xs text-muted-foreground">
          {metric.unit === "eur" ? "EUR" : metric.unit}
        </span>
      </div>
      {query.isError ? (
        <p role="alert" className="chart-message">
          Chart unavailable. Retrying…
        </p>
      ) : query.isPending ? (
        <p className="chart-message">Loading chart…</p>
      ) : !points.length ? (
        <p className="chart-message">No measurements in this window.</p>
      ) : (
        <>
          <div className="h-40 mt-4" aria-hidden="true">
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <LineChart
                data={points}
                margin={{ top: 5, right: 5, bottom: 0, left: -22 }}
              >
                <CartesianGrid vertical={false} stroke="#e8ebf0" />
                <XAxis
                  dataKey="ts"
                  tickFormatter={(value) => time(String(value)).slice(-5)}
                  minTickGap={45}
                  tick={{ fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tick={{ fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                  width={60}
                  tickFormatter={(value) =>
                    Intl.NumberFormat("en", { notation: "compact" }).format(
                      Number(value),
                    )
                  }
                />
                <Tooltip
                  labelFormatter={(value) => `${time(String(value))} UTC`}
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
              </LineChart>
            </ResponsiveContainer>
          </div>
          <p className="text-xs text-muted-foreground mt-2">
            Actual{" "}
            {latest?.value?.toLocaleString(undefined, {
              maximumFractionDigits: 2,
            }) ?? "missing"}{" "}
            · expected{" "}
            {latest?.expected?.toLocaleString(undefined, {
              maximumFractionDigits: 2,
            }) ?? "unknown"}
            . Dashed line: expected.
          </p>
        </>
      )}
    </section>
  );
}
