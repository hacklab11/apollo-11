import type { Incident, Metric } from './api'

export function group(incident: Incident): 'Open' | 'Recovered' | 'Closed' {
  return incident.status === 'closed' ? 'Closed' : incident.status === 'recovered' ? 'Recovered' : 'Open'
}

export function chartMetrics(incident: Incident, metrics: Metric[]) {
  const names = [...new Set([
    incident.entry.metric, incident.root_cause?.root_node,
    ...(incident.symptoms ?? []).map(s => s.node),
  ])].filter((name): name is string => !!name)
  return names.flatMap(name => {
    const metric = metrics.find(m => m.name === name)
    return metric ? [metric] : []
  }).slice(0, 3)
}

export function time(value: string | null | undefined) {
  if (!value) return 'not available'
  return new Intl.DateTimeFormat('en-GB', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
  }).format(new Date(value))
}

export const label = (name: string) => name.replaceAll('_', ' ')
export const money = (value: number) => new Intl.NumberFormat('en-IE', {
  style: 'currency', currency: 'EUR', maximumFractionDigits: 0,
}).format(value)
