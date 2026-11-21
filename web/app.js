const mockMode = new URLSearchParams(location.search).get("mock") === "1";
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[char],
  );
const readable = (value) => String(value ?? "").replaceAll("_", " ");
const number = (value) =>
  new Intl.NumberFormat("en-GB", { maximumFractionDigits: 1 }).format(value);
const euro = (value) =>
  new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 0,
  }).format(value);
const signed = (value) =>
  `${value > 0 ? "+" : value < 0 ? "−" : ""}${number(Math.abs(value))}`;
const time = (value) =>
  new Date(value).toLocaleTimeString("en-GB", {
    timeZone: "UTC",
    hour: "2-digit",
    minute: "2-digit",
  });
const badge = (verdict) =>
  verdict
    ? `<span class="badge verdict-${escapeHtml(verdict)}">${escapeHtml(verdict)}</span>`
    : '<span class="badge neutral">Pending</span>';
const actorLetters = {
  sentinel: "S",
  detective: "D",
  skeptic: "K",
  reporter: "R",
  cartographer: "C",
  growth: "G",
  marketplace: "M",
  discovery: "D",
  selleros: "X",
  orchestrator: "O",
  chat: "C",
};

const fixtures = new Map();
async function fixture(file, json = true) {
  if (!fixtures.has(file)) {
    fixtures.set(
      file,
      fetch(new URL(`mock/${file}`, import.meta.url))
        .then(async (response) => {
          if (!response.ok)
            throw new Error(`Could not load ${file} (${response.status}).`);
          return json ? response.json() : response.text();
        })
        .catch((error) => {
          fixtures.delete(file);
          throw error;
        }),
    );
  }
  return structuredClone(await fixtures.get(file));
}

let mockStatus;
let mockScenarios;

const mockApi = {
  async getStatus() {
    return (mockStatus ??= await fixture("status.json"));
  },
  getGraph: () => fixture("graph.json"),
  getMetrics: () => fixture("metrics.json"),
  getMetric: (_name, _query = {}) => fixture("metric_orders.json"),
  async getMetricDefinition(name) {
    if (name === "orders") return fixture("metric_def_orders.json");
    const [metrics, graph] = await Promise.all([
      api.getMetrics(),
      api.getGraph(),
    ]);
    return {
      ...metrics.find((metric) => metric.name === name),
      in_edges: graph.edges.filter((edge) => edge.dst === name),
      out_edges: graph.edges.filter((edge) => edge.src === name),
    };
  },
  async addMetric() {
    const result = await fixture("add_metric_result.json");
    const name = "fee_per_order";
    const expression = "fee_revenue / orders";
    Object.assign(result.metric, {
      name, expression, description: "Marketplace fee revenue in euros divided by units ordered.",
      unit: "eur", dimensions: ["country", "platform", "category", "seller_tier", "channel"],
    });
    result.accepted = result.accepted.map((edge, index) => {
      const src = ["fee_revenue", "orders"][index];
      return { ...edge, id: `edg_${src}_${name}`, src, dst: name, identity: `${name} = ${expression}` };
    });
    result.notes = "Mock formula: fee revenue divided by orders. No query was executed.";
    result.trail_investigation_id = "inv_add_fee_per_order";
    return result;
  },
  getAnomalies: (_since) => fixture("anomalies.json"),
  async getIncidents(status) {
    const incidents = await fixture("incidents.json");
    return status
      ? incidents.filter((incident) => incident.status === status)
      : incidents;
  },
  getIncident: (id) => {
    const files = { inc_s7: "incident_s7.json", inc_s6: "incident_s6.json" };
    if (!files[id])
      return Promise.reject(new Error(`No incident fixture for ${id}.`));
    return fixture(files[id]);
  },
  async getTrail(id) {
    if (id === "inc_s7") return fixture("trail_s7.json");
    // T00 has no trail_s6.json; its real TrailStep is present in the event fixture.
    const [incident, events] = await Promise.all([
      api.getIncident(id),
      api.getEvents(),
    ]);
    return events
      .filter(
        (event) =>
          event.type === "trail_step" &&
          incident.investigation_ids.includes(event.payload.investigation_id),
      )
      .map((event) => event.payload);
  },
  async getScenarios() {
    return (mockScenarios ??= await fixture("scenarios.json"));
  },
  async injectScenario(id) {
    return setScenario(id, true);
  },
  async clearScenario(id) {
    return setScenario(id, false);
  },
  async pauseClock() {
    (await api.getStatus()).paused = true;
    return mockStatus;
  },
  async resumeClock() {
    (await api.getStatus()).paused = false;
    return mockStatus;
  },
  async stepClock() {
    const status = await api.getStatus();
    status.sim_ts = new Date(
      Date.parse(status.sim_ts) + 3_600_000,
    ).toISOString();
    status.tick += 1;
    return status;
  },
  async getEvents() {
    return (await fixture("events.jsonl", false))
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line));
  },
  chat: (_messages) => fixture("chat_response.json"),
  getTruth: () => fixture("truth.json"),
};

let streamConnected = false;

async function request(path, { method = "GET", query = {}, body } = {}) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value != null)
      search.set(key, typeof value === "object" ? JSON.stringify(value) : value);
  }
  let response;
  try {
    response = await fetch(`/api${path}${search.size ? `?${search}` : ""}`, {
      method,
      ...(body === undefined ? {} : {
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    });
  } catch {
    $("#core-notice").hidden = false;
    throw new Error("core not reachable");
  }
  if (!response.ok) {
    const failure = await response.json().catch(() => null);
    throw new Error(
      typeof failure?.detail === "string"
        ? failure.detail
        : `${method} ${path} failed (${response.status}).`,
    );
  }
  if (path === "/status" && streamConnected) $("#core-notice").hidden = true;
  return response.json();
}

const post = (path, body) => request(path, { method: "POST", body });
const metricPath = (name) => `/metrics/${encodeURIComponent(name)}`;
const incidentPath = (id) => `/incidents/${encodeURIComponent(id)}`;

export const api = mockMode ? mockApi : {
  getStatus: () => request("/status"),
  getGraph: (query) => request("/graph", { query }),
  getMetrics: () => request("/metrics"),
  getMetric: (name, query) => request(metricPath(name), { query }),
  getMetricDefinition: (name) => request(`${metricPath(name)}/definition`),
  addMetric: (body) => post("/metrics", body),
  getAnomalies: (since) => request("/anomalies", { query: { since } }),
  getIncidents: (status) => request("/incidents", { query: { status } }),
  getIncident: (id) => request(incidentPath(id)),
  getTrail: (id) => request(`${incidentPath(id)}/trail`),
  getScenarios: () => request("/scenarios"),
  injectScenario: (id) => post(`/scenarios/${encodeURIComponent(id)}/inject`),
  clearScenario: (id) => post(`/scenarios/${encodeURIComponent(id)}/clear`),
  pauseClock: () => post("/clock/pause"),
  resumeClock: () => post("/clock/resume"),
  stepClock: () => post("/clock/step"),
  chat: mockApi.chat,
  getTruth: () => request("/demo/truth"),
};

async function setScenario(id, active) {
  const scenarios = await api.getScenarios();
  const scenario = scenarios.find((item) => item.id === id);
  if (!scenario) throw new Error(`Unknown scenario ${id}.`);
  scenario.active = active;
  scenario[active ? "injected_ts" : "cleared_ts"] = (
    await api.getStatus()
  ).sim_ts;
  return scenario;
}

const state = {
  status: null,
  graph: null,
  metrics: [],
  incidents: [],
  incident: null,
  selectedMetric: "orders",
  filter: "open",
  events: [],
  eventIndex: 0,
  messages: [],
  selectedRequest: 0,
  seriesRequest: 0,
  incidentListRequest: 0,
  graphRequest: 0,
  openingIncident: null,
  chart: null,
  cy: null,
  trail: [],
  attachedEvidence: new Map(),
  newEdges: new Set(),
};

function notify(message) {
  $("#notice").textContent = message;
  $("#notice").hidden = false;
}

async function attempt(action) {
  try {
    await action();
  } catch (error) {
    if (error.message !== "core not reachable") notify(error.message);
  }
}

function renderStatus() {
  const status = state.status;
  $("#sim-clock").textContent =
    `${new Date(status.sim_ts).toLocaleDateString("en-GB", { timeZone: "UTC", day: "2-digit", month: "short", year: "numeric" })} · ${time(status.sim_ts)} · tick ${status.tick}`;
  $("#sim-clock").dateTime = status.sim_ts;
  $("#clock-toggle").textContent = status.paused ? "▶ Resume" : "Ⅱ Pause";
  $("#clock-toggle").setAttribute(
    "aria-label",
    status.paused
      ? "Resume simulation"
      : "Pause simulation",
  );
  $("#llm-mode").textContent = String(status.llm_mode).toUpperCase();
  $("#usage").textContent =
    `${status.llm_calls} calls · ${number(status.tokens_in + status.tokens_out)} tokens`;
  $("#agents").innerHTML = Object.entries(status.agents)
    .map(
      ([name, agent]) =>
        `<span class="agent"><i class="dot ${agent.ok ? "healthy" : "incident"}" aria-hidden="true"></i>${escapeHtml(name)}<span class="sr-only">${agent.ok ? " healthy" : " unavailable"}</span></span>`,
    )
    .join("");
}

function updateStatus(status, reset = false) {
  if (!reset && state.status && status.tick < state.status.tick) return;
  state.status = status;
  renderStatus();
}

function renderScenarios(scenarios) {
  const byId = new Map(scenarios.map((scenario) => [scenario.id, scenario]));
  const ids = new Set(["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "I1", "I2", "I3", ...byId.keys()]);
  const titles = { S6: "Black Friday cascade", S8: "The €9 headphones", I3: "Electronics coupon day" };
  $("#scenarios").innerHTML = [...ids].map((id) => {
    const scenario = byId.get(id);
    const key = escapeHtml(id);
    if (!scenario) {
      const label = `${key}${titles[id] ? `: ${titles[id]}` : ""} (unavailable)`;
      return `<button type="button" data-scenario="${key}" aria-label="${label}" title="${label}" disabled>${key}</button>`;
    }
    const title = escapeHtml(scenario.title);
    return `<button type="button" data-scenario="${key}" aria-pressed="${scenario.active}" aria-label="${scenario.active ? "Clear" : "Inject"} ${key}: ${title}" title="${title}">${key}${scenario.active ? " ·" : ""}</button>`;
  }).join("");
}

function renderScenario(scenario) {
  const button = [...$("#scenarios").children].find(
    (item) => item.dataset.scenario === scenario.id,
  );
  if (!button) return;
  button.disabled = false;
  button.title = scenario.title;
  button.setAttribute("aria-pressed", String(scenario.active));
  button.setAttribute(
    "aria-label",
    `${scenario.active ? "Clear" : "Inject"} ${scenario.id}: ${scenario.title}`,
  );
  button.textContent = `${scenario.id}${scenario.active ? " ·" : ""}`;
}

function graphElements() {
  return [
    ...state.graph.nodes.map((node) => ({
      data: { ...node, displayLabel: node.label },
      group: "nodes",
    })),
    ...state.graph.edges
      .filter((edge) => edge.type !== "breakdown")
      .map((edge) => ({
        group: "edges",
        data: {
          ...edge,
          source: edge.src,
          target: edge.dst,
          label: edge.type === "formula" ? "=" : "",
          weight: 1 + edge.confidence * 2.5,
        },
      })),
  ];
}

function renderGraph() {
  $("#node-count").textContent = state.graph.nodes.length;
  $("#metric-select").innerHTML = state.graph.nodes
    .map(
      (node) =>
        `<option value="${escapeHtml(node.id)}">${escapeHtml(node.label)} · ${escapeHtml(node.state)}</option>`,
    )
    .join("");
  $("#metric-select").value = state.selectedMetric;
  if (!globalThis.cytoscape) {
    notify(
      "The graph library could not load from the CDN. The metric selector still works; check your connection and reload.",
    );
    return;
  }
  if (!state.cy) {
    state.cy = cytoscape({
      container: $("#graph"),
      elements: [],
      minZoom: 0.25,
      maxZoom: 3,
      wheelSensitivity: 0.2,
      style: [
        {
          selector: "node",
          style: {
            label: "data(displayLabel)",
            "font-family": "DM Sans, sans-serif",
            "font-size": 11,
            "text-wrap": "wrap",
            "text-max-width": 96,
            "text-valign": "center",
            "text-halign": "center",
            color: "#172f39",
            shape: "roundrectangle",
            width: 112,
            height: 42,
            "background-color": "#e1e7e9",
            "border-width": 2,
            "border-color": "#a8b2b8",
            "transition-property": "background-color, border-color",
            "transition-duration": "300ms",
          },
        },
        {
          selector: 'node[state="watch"]',
          style: { "background-color": "#f3cf83", "border-color": "#996717" },
        },
        {
          selector: 'node[state="anomaly"]',
          style: {
            "background-color": "#fff",
            "border-color": "#b7323e",
            "border-width": 3,
          },
        },
        {
          selector: 'node[state="incident"]',
          style: {
            "background-color": "#b7323e",
            "border-color": "#b7323e",
            color: "#ffffff",
          },
        },
        {
          selector: 'node[state="resolved"]',
          style: {
            "background-color": "#fff",
            "border-color": "#126749",
            "border-width": 3,
          },
        },
        {
          selector: "edge",
          style: {
            width: "data(weight)",
            "curve-style": "bezier",
            "line-color": "#78878e",
            "target-arrow-color": "#78878e",
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.65,
            label: "data(label)",
            "font-size": 12,
            color: "#354b56",
            "text-background-color": "#fff",
            "text-background-opacity": 1,
            "text-background-padding": "2px",
          },
        },
        {
          selector: 'edge[type="formula"]',
          style: {
            "line-color": "#354b56",
            "target-arrow-color": "#354b56",
            "line-style": "solid",
            width: 1.5,
          },
        },
        {
          selector: 'edge[type="causal_hypothesis"]',
          style: { "line-style": "dashed" },
        },
        {
          selector: 'edge[status="ambiguous"]',
          style: {
            "line-style": "dotted",
            "line-color": "#a46b0d",
            "target-arrow-color": "#a46b0d",
          },
        },
        {
          selector: 'edge[status="rejected"]',
          style: {
            display: "none",
            "line-color": "#a46b0d",
            "line-style": "dotted",
          },
        },
        {
          selector: ".new-edge",
          style: {
            "underlay-color": "#5bb58a",
            "underlay-opacity": 0.25,
            "underlay-padding": 4,
          },
        },
        {
          selector: "node:selected",
          style: {
            "underlay-color": "#9bb8ad",
            "underlay-opacity": 0.35,
            "underlay-padding": 7,
          },
        },
      ],
    });
    state.cy.on("tap", "node", (event) =>
      attempt(() => selectMetric(event.target.id())),
    );
    state.cy.on("tap", "edge", (event) => {
      const edge = event.target.data();
      $("#edge-description").textContent =
        edge.identity ??
        `${readable(edge.type)} · confidence ${number(edge.confidence * 100)}% · lag ${edge.lag_hours ?? 0}h · ${edge.status}`;
    });
    new ResizeObserver(() => {
      state.cy.resize();
      state.cy.fit(undefined, 30);
    }).observe($("#graph"));
  }
  state.cy.json({ elements: graphElements() });
  state.cy
    .layout({
      name: "grid",
      animate: false,
      cols: 2,
      avoidOverlap: true,
      nodeDimensionsIncludeLabels: true,
      padding: 20,
    })
    .run();
  state.cy.nodes().unselect();
  state.cy.getElementById(state.selectedMetric).select();
  for (const id of state.newEdges)
    state.cy.getElementById(id).addClass("new-edge");
  setRejectedVisibility();
}

async function refreshGraph() {
  const request = ++state.graphRequest;
  const [graph, metrics] = await Promise.all([api.getGraph(), api.getMetrics()]);
  if (request !== state.graphRequest) return;
  const firstGraph = !state.graph;
  Object.assign(state, { graph, metrics });
  renderGraph();
  renderIncidents();
  if (graph.nodes.length && (firstGraph || !graph.nodes.some((node) => node.id === state.selectedMetric)))
    await selectMetric($("#metric-select").value || graph.nodes[0].id);
}

function setRejectedVisibility() {
  state.cy
    ?.edges('[status="rejected"]')
    .style("display", $("#show-rejected").checked ? "element" : "none");
}

async function selectMetric(name) {
  const request = ++state.selectedRequest;
  const seriesRequest = ++state.seriesRequest;
  state.selectedMetric = name;
  $("#metric-select").value = name;
  state.cy?.nodes().unselect();
  state.cy?.getElementById(name).select();
  const node = state.graph?.nodes.find((item) => item.id === name);
  $("#metric-title").textContent = node?.label ?? readable(name);
  const [series, definition] = await Promise.all([
    api.getMetric(name),
    api.getMetricDefinition(name),
  ]);
  if (request !== state.selectedRequest) return;
  const metric = state.metrics.find((item) => item.name === name) ?? definition;
  $("#metric-description").textContent =
    metric.description || "Added by the Cartographer.";
  $("#dimension-chips").innerHTML = (metric.dimensions ?? [])
    .map((dimension) => `<span class="chip">${escapeHtml(dimension)}</span>`)
    .join("");
  $("#edge-description").textContent =
    metric.expression ||
    definition.sql ||
    `${definition.in_edges?.length ?? 0} incoming · ${definition.out_edges?.length ?? 0} outgoing relationships`;
  const reference = series.metric !== name;
  $("#metric-unit").textContent = reference
    ? "ORDERS REFERENCE"
    : `${metric.unit ?? "count"} / hour`;
  $("#fixture-note").textContent = reference
    ? `No ${readable(name)} series fixture exists. Showing the Orders reference, not measurements for this metric.`
    : `Actual vs expected · shaded forecast range${mockMode ? " · release marker at 27 Nov, 03:00 UTC" : ""}`;
  if (seriesRequest === state.seriesRequest) renderChart(series);
}

async function refreshChart() {
  const name = state.selectedMetric;
  if (!state.graph?.nodes.some((node) => node.id === name)) return;
  const request = ++state.seriesRequest;
  const series = await api.getMetric(name);
  if (request === state.seriesRequest) renderChart(series);
}

function renderChart(series) {
  const points = series.points;
  const last = points.at(-1);
  const delta =
    last?.value != null && last.expected
      ? ((last.value - last.expected) / last.expected) * 100
      : null;
  $("#chart-summary").textContent =
    `${readable(series.metric)}: latest ${last?.value == null ? "no data" : number(last.value)}, expected ${last?.expected == null ? "unavailable" : number(last.expected)}${delta === null ? "" : ` (${signed(delta)}%)`}. Shading shows the expected lower–upper range.${mockMode ? " Vertical marker: checkout-web v5.8 release." : ""}`;
  $("#metric-chart").setAttribute(
    "aria-label",
    $("#chart-summary").textContent,
  );
  if (!globalThis.Chart) {
    notify(
      "The chart library could not load from the CDN. The chart summary remains available; check your connection and reload.",
    );
    return;
  }
  state.chart?.destroy();
  state.chart = new Chart($("#metric-chart"), {
    type: "line",
    data: {
      labels: points.map((point) => point.ts),
      datasets: [
        {
          label: "Lower bound",
          data: points.map((point) => point.lower),
          borderWidth: 0,
          pointRadius: 0,
          fill: false,
        },
        {
          label: "Expected range",
          data: points.map((point) => point.upper),
          borderWidth: 0,
          pointRadius: 0,
          backgroundColor: "#dcebe480",
          fill: "-1",
        },
        {
          label: "Expected",
          data: points.map((point) => point.expected),
          borderColor: "#648375",
          borderDash: [4, 4],
          borderWidth: 1.5,
          pointRadius: 0,
        },
        {
          label: `Actual ${readable(series.metric)}`,
          data: points.map((point) => point.value),
          borderColor: "#b7323e",
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            color: "#536775",
            boxWidth: 12,
            boxHeight: 3,
            font: { size: 9 },
            filter: (item) => item.text !== "Lower bound",
          },
        },
        tooltip: {
          callbacks: {
            title: (items) =>
              `${new Date(items[0].label).toLocaleString("en-GB", { timeZone: "UTC" })} UTC`,
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            maxTicksLimit: 5,
            maxRotation: 0,
            color: "#536775",
            font: { size: 9 },
            callback: (value) => {
              const ts = points[value]?.ts;
              return ts && `${new Date(ts).toLocaleDateString("en-GB", { timeZone: "UTC", day: "numeric", month: "short" })} ${time(ts)}`;
            },
          },
        },
        y: {
          ticks: { maxTicksLimit: 4, color: "#536775", font: { size: 9 } },
          grid: { color: "#edf1f1" },
          border: { display: false },
        },
      },
    },
    plugins: mockMode ? [
      {
        id: "release-marker",
        afterDatasetsDraw(chart) {
          const index = points.findIndex(
            (point) =>
              Date.parse(point.ts) >= Date.parse("2026-11-27T03:00:00Z"),
          );
          if (index < 0) return;
          const x = chart.scales.x.getPixelForValue(index);
          const { ctx, chartArea: area } = chart;
          ctx.save();
          ctx.setLineDash([3, 3]);
          ctx.strokeStyle = "#8a5a09";
          ctx.beginPath();
          ctx.moveTo(x, area.top + 13);
          ctx.lineTo(x, area.bottom);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = "#805309";
          ctx.font = "9px sans-serif";
          ctx.fillText(
            "v5.8 release",
            Math.min(x + 4, area.right - 64),
            area.top + 9,
          );
          ctx.restore();
        },
      },
    ] : [],
  });
}

function filteredIncidents() {
  const statuses = {
    open: ["investigating", "open", "persisting"],
    cleared: ["innocent", "deferred"],
    resolved: ["resolved"],
  };
  return state.incidents.filter((incident) =>
    statuses[state.filter].includes(incident.status),
  );
}

function renderIncidents() {
  const focusedIncident = document.activeElement?.dataset.incident;
  const incidents = filteredIncidents();
  $("#incident-count").textContent = `${incidents.length} ${state.filter}`;
  $("#incidents-list").innerHTML = incidents.length
    ? incidents
        .map(
          (incident) =>
            `<button type="button" class="incident-row" data-incident="${escapeHtml(incident.id)}" aria-pressed="${state.incident?.id === incident.id}"><i class="dot incident" aria-hidden="true"></i><span class="row-title">${escapeHtml(incident.title)}<small>${escapeHtml(incident.id)} · ${time(incident.opened_ts)} UTC · ${euro(incident.impact_eur_per_hour)}/h</small></span>${badge(incident.verdict)}</button>`,
        )
        .join("")
    : `<p class="empty">${state.filter === "open" ? `No open incidents. The system is watching ${state.metrics.length} metrics.` : `No ${escapeHtml(state.filter)} incidents.`}</p>`;
  if (focusedIncident) {
    [...document.querySelectorAll("[data-incident]")]
      .find((button) => button.dataset.incident === focusedIncident)
      ?.focus();
  }
}

async function openIncident(id, focus = false, snapshot = null) {
  const resetScroll = state.incident?.id !== id;
  const opening = { id, focus, incident: snapshot, investigationIds: new Set(), trail: [] };
  state.openingIncident = opening;
  try {
    const [incident, trail] = await Promise.all([
      snapshot ?? api.getIncident(id),
      api.getTrail(id),
    ]);
    if (state.openingIncident !== opening) return;
    state.incident = opening.incident ?? incident;
    state.incident.investigation_ids = [...new Set([
      ...state.incident.investigation_ids,
      ...opening.investigationIds,
    ])];
    state.trail = [...new Map([
      ...trail,
      ...opening.trail.filter((step) =>
        state.incident.investigation_ids.includes(step.investigation_id),
      ),
    ].map((step) => [step.id, step])).values()];
    renderIncidents();
    renderIncidentDetail(resetScroll);
    if (focus) {
      $("#incident-detail").tabIndex = -1;
      $("#incident-detail").focus();
    }
  } finally {
    if (state.openingIncident === opening) state.openingIncident = null;
  }
}

async function refreshIncidents(reconnect = false) {
  const request = ++state.incidentListRequest;
  const incidents = await api.getIncidents();
  if (request !== state.incidentListRequest) return;
  state.incidents = incidents;
  if (reconnect && state.incident && !incidents.some((item) => item.id === state.incident.id)) {
    state.incident = null;
    renderIncidentDetail();
  }
  renderIncidents();
  const id = reconnect && state.incident
    ? state.incident.id
    : !state.incident && filteredIncidents()[0]?.id;
  if (id && !state.openingIncident) await openIncident(id);
}

function renderIncidentDetail(resetScroll = true) {
  const incident = state.incident;
  if (!incident) {
    $("#incident-detail").innerHTML = "";
    return;
  }
  const contributions = incident.contributions
    .map((factor) => {
      // share is relative to the signed total delta, not the sign of the factor.
      const share = Math.abs(factor.share * 100) * Math.sign(factor.delta);
      return `<div class="contribution"><div class="factor-label"><span>${escapeHtml(readable(factor.factor))}${factor.kind === "planned" ? " · planned" : ""}</span><strong>${signed(share)}%</strong></div><div class="bar-track"><div class="bar-fill ${factor.delta > 0 ? "positive" : ""} ${factor.kind === "planned" ? "planned" : ""}" style="width:${Math.min(100, Math.abs(factor.share * 100))}%"></div></div><span class="sr-only">Delta ${signed(factor.delta)}; contribution ${signed(share)} percent.</span></div>`;
    })
    .join("");
  $("#incident-detail").innerHTML =
    `<div class="detail-meta">${badge(incident.verdict)}<span>${escapeHtml(incident.id)} · ${escapeHtml(incident.status)} · Occurrences: ${number(incident.occurrences)}</span></div><h4>${escapeHtml(incident.title)}</h4><p class="story">${escapeHtml(incident.summary)}</p><section class="attached-evidence" hidden></section><div class="impact">${euro(incident.impact_eur_per_hour)}<small> GMV impact / hour</small></div><div class="detail-meta">Affected: ${incident.affected_actors.map(escapeHtml).join(", ")}</div><h5 class="section-label">What explains the change</h5>${contributions}<h5 class="section-label">Hypotheses & evidence</h5>${incident.hypotheses.map((hypothesis) => `<details class="hypothesis ${escapeHtml(hypothesis.status)}"><summary><span class="badge">${escapeHtml(hypothesisLabel(hypothesis.status))}</span>${escapeHtml(hypothesis.statement)}</summary>${hypothesis.evidence_for.map((evidence) => `<p><strong>For · ${escapeHtml(evidence.source)}:</strong> ${escapeHtml(evidence.summary)}</p>`).join("")}${hypothesis.evidence_against.map((evidence) => `<p><strong>Against · ${escapeHtml(evidence.source)}:</strong> ${escapeHtml(evidence.summary)}</p>`).join("")}</details>`).join("")}<div class="action-box"><strong>Suggested next move</strong>${incident.suggested_actions.map(escapeHtml).join("<br>")}</div>${incident.notifications.map(renderNotification).join("")}${incident.open_questions.length ? `<details class="hypothesis"><summary>Open questions (${incident.open_questions.length})</summary>${incident.open_questions.map((question) => `<p>${escapeHtml(question)}</p>`).join("")}</details>` : ""}<h5 class="section-label">Investigation trail · ${state.trail.length} steps</h5><ol class="timeline">${state.trail.map(renderTrailStep).join("")}</ol>`;
  renderAttachedEvidence();
  if (resetScroll) $("#incident-detail").scrollTop = 0;
}

function hypothesisLabel(status) {
  return status === "open" ? "Hypothesis" : readable(status);
}

function renderNotification(notification) {
  const actions = notification.ack?.actions_taken ?? [];
  return `<div class="notification">SellerOS notification · ${notification.ok ? "Delivered" : "Failed"} · ${time(notification.ts)} UTC${notification.task_id ? `<br><span class="muted">${escapeHtml(notification.task_id)}</span>` : ""}${actions.length ? `<ul>${actions.map((action) => `<li>${escapeHtml(action)}</li>`).join("")}</ul>` : ""}</div>`;
}

function renderAttachedEvidence() {
  const section = $("#incident-detail .attached-evidence");
  if (!section) return;
  const evidence = [...state.attachedEvidence.values()]
    .filter((decision) => decision.attach_to_incident === state.incident.id);
  section.hidden = !evidence.length;
  section.innerHTML = evidence.length
    ? `<h5 class="section-label">Attached evidence</h5><ul>${evidence.map(({ anomaly, reason }) => {
        const segment = Object.entries(anomaly.segment).map(([key, value]) => `${key}: ${value}`).join(", ");
        return `<li><strong>${escapeHtml(readable(anomaly.metric))}</strong> · ${time(anomaly.ts)} UTC${segment ? ` · ${escapeHtml(segment)}` : ""} — ${escapeHtml(reason)}</li>`;
      }).join("")}</ul>`
    : "";
}

function rememberAttachments(decisions) {
  for (const decision of decisions) {
    if (decision.action === "attach" && decision.attach_to_incident)
      state.attachedEvidence.set(decision.anomaly.id, decision);
  }
  renderAttachedEvidence();
}

function renderTrailEvidence(step) {
  const output = step.output;
  if (!output) return "";
  if (step.action === "a2a_call" && step.tool === "selleros" && output.answered && output.findings?.length) {
    return `${step.input.question ? `<p><strong>Question:</strong> ${escapeHtml(step.input.question)}</p>` : ""}<p><strong>SellerOS answer</strong></p><ul>${output.findings.map(({ claim }) => `<li>${escapeHtml(claim)}</li>`).join("")}</ul>`;
  }
  const cause = step.action === "decision" ? output.root_cause : null;
  if (cause) {
    const label = { supported: "Supported cause", refuted: "Refuted cause hypothesis" }[cause.status] ?? "Cause hypothesis";
    return `<p><strong>${label}:</strong> ${escapeHtml(cause.statement)}</p>`;
  }
  if (step.tool === "update_hypotheses" && output.hypotheses?.length) {
    return `<ul>${output.hypotheses.map(({ status, statement }) => `<li><strong>${escapeHtml(hypothesisLabel(status))}:</strong> ${escapeHtml(statement)}</li>`).join("")}</ul>`;
  }
  return "";
}

function renderTrailStep(step) {
  return `<li class="trail-step" data-step="${escapeHtml(step.id)}"><span class="actor-icon" aria-hidden="true">${actorLetters[step.actor] ?? "?"}</span><div><div class="trail-meta"><strong>${step.n}. ${escapeHtml(step.actor)}</strong>${step.actor === "selleros" || step.action === "a2a_call" && step.tool === "selleros" ? '<span class="external">external</span>' : ""}<span class="tool">${escapeHtml(step.tool ?? step.action)}</span></div><p>${escapeHtml(step.output_summary)}</p>${renderTrailEvidence(step)}<p class="reason">Why: ${escapeHtml(step.reason)}</p><details class="trail-json"><summary>Step details</summary><pre>${escapeHtml(JSON.stringify({ action: step.action, input: step.input, output: step.output, duration_ms: step.duration_ms }, null, 2))}</pre></details></div></li>`;
}

function appendTrailStep(step) {
  state.openingIncident?.trail.push(step);
  if (
    !state.incident?.investigation_ids.includes(step.investigation_id) ||
    state.trail.some((item) => item.id === step.id)
  ) return;
  state.trail.push(step);
  const timeline = $("#incident-detail .timeline");
  timeline.insertAdjacentHTML("beforeend", renderTrailStep(step));
  timeline.previousElementSibling.textContent =
    `Investigation trail · ${state.trail.length} steps`;
}

function appendEvent(event) {
  const payload = event.payload;
  const summary =
    payload.output_summary ??
    payload.reason ??
    payload.title ??
    ({
      tick: () => `Simulation tick ${payload.tick}`,
      graph_changed: () => "Metric relationships updated",
      scenario_changed: () => `${payload.id} ${payload.active ? "active" : "inactive"}`,
      agent_status: () => `${payload.name} ${payload.ok ? "healthy" : "unavailable"}`,
    }[event.type]?.() ??
      (payload.metric
        ? `${readable(payload.metric)} · ${payload.kind ?? ""}`
        : `Investigation ${payload.investigation_id ?? payload.id ?? ""}`));
  const item = document.createElement("li");
  item.innerHTML = `<time datetime="${escapeHtml(event.sim_ts)}">${time(event.sim_ts)} UTC</time><strong>${escapeHtml(readable(event.type))}</strong><span>${escapeHtml(summary)}</span>`;
  $("#feed").append(item);
  while ($("#feed").children.length > 30) $("#feed").firstElementChild.remove();
  $("#feed").scrollTop = $("#feed").scrollHeight;
}

function replayNext() {
  if (!state.events.length) return;
  appendEvent(state.events[state.eventIndex]);
  state.eventIndex = (state.eventIndex + 1) % state.events.length;
}

async function handleLiveEvent(event) {
  appendEvent(event);
  const { payload } = event;
  switch (event.type) {
    case "sentinel_decision":
      if (payload.action === "attach") rememberAttachments([payload]);
      break;
    case "tick":
      if (state.status)
        updateStatus({ ...state.status, tick: payload.tick, sim_ts: event.sim_ts });
      await Promise.all([api.getStatus().then((status) => updateStatus(status)), refreshChart()]);
      break;
    case "incident_upserted": {
      const opening = state.openingIncident;
      const isOpening = opening?.id === payload.id;
      const current = state.incident?.id === payload.id ? state.incident : null;
      const previous = isOpening ? opening.incident ?? current : current;
      const needsTrail = (isOpening || (current && !opening)) &&
        payload.investigation_ids.some((id) => !previous?.investigation_ids.includes(id));
      if (isOpening) opening.incident = payload;
      if (current) {
        state.incident = payload;
        renderIncidentDetail(false);
      }
      await Promise.all([
        needsTrail && openIncident(payload.id, isOpening && opening.focus, payload),
        refreshIncidents(),
      ]);
      break;
    }
    case "investigation_started":
      if (state.openingIncident?.id === payload.incident_id)
        state.openingIncident.investigationIds.add(payload.investigation_id);
      if (
        state.incident?.id === payload.incident_id &&
        !state.incident.investigation_ids.includes(payload.investigation_id)
      ) state.incident.investigation_ids.push(payload.investigation_id);
      break;
    case "trail_step":
      appendTrailStep(payload);
      break;
    case "graph_changed":
      await refreshGraph();
      break;
    case "scenario_changed":
      renderScenario(payload);
      break;
    case "agent_status":
      if (state.status) {
        state.status.agents[payload.name] = payload;
        renderStatus();
      }
      break;
  }
}

async function refreshLive(reconnect = false) {
  const graph = refreshGraph();
  await Promise.all([
    api.getStatus().then((status) => updateStatus(status, reconnect)),
    graph,
    refreshIncidents(reconnect),
    api.getAnomalies().then(rememberAttachments),
    api.getScenarios().then(renderScenarios),
    reconnect && graph.then(refreshChart),
  ]);
}

function connectEvents(reconnect = false) {
  const stream = new EventSource("/api/events");
  const receive = (message) => attempt(() => handleLiveEvent(JSON.parse(message.data)));
  for (const type of [
    "tick", "anomaly", "sentinel_decision", "investigation_started",
    "trail_step", "investigation_finished", "incident_upserted",
    "graph_changed", "scenario_changed", "agent_status",
  ]) stream.addEventListener(type, receive);
  stream.onopen = () => {
    streamConnected = true;
    $("#core-notice").hidden = true;
    if (reconnect) attempt(() => refreshLive(true));
  };
  stream.onerror = () => {
    streamConnected = false;
    stream.close();
    $("#core-notice").hidden = false;
    setTimeout(() => connectEvents(true), 2000);
  };
}

function renderMessage(role, content) {
  const message = document.createElement("div");
  message.className = `message ${role}`;
  message.innerHTML = `<span class="message-label">${role === "user" ? "You" : "Eye of the System"}</span><div>${escapeHtml(
    content,
  )
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replaceAll("\n", "<br>")}</div>`;
  $("#messages").append(message);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return message;
}

async function sendChat(event) {
  event.preventDefault();
  const input = $("#chat-input");
  const content = input.value.trim();
  if (!content) return;
  $("#messages .welcome")?.remove();
  state.messages.push({ role: "user", content });
  renderMessage("user", content);
  input.value = "";
  $("#chat-submit").disabled = true;
  $("#chat-submit").textContent = "Thinking…";
  $("#messages").setAttribute("aria-busy", "true");
  try {
    const response = await api.chat(state.messages.slice(-20));
    state.messages.push({ role: "assistant", content: response.answer });
    const message = renderMessage("assistant", response.answer);
    message.insertAdjacentHTML(
      "beforeend",
      `<details><summary>Tool calls (${response.tool_calls.length})</summary>${response.tool_calls.map((call) => `<p><strong>${escapeHtml(call.tool)}</strong> · ${escapeHtml(call.summary)}</p><pre>${escapeHtml(JSON.stringify(call.input, null, 2))}</pre>`).join("")}</details>${mockMode ? response.incident_ids.map((id) => `<button class="incident-link" type="button" data-chat-incident="${escapeHtml(id)}">View ${escapeHtml(id)} ↗</button>`).join("") : ""}`,
    );
    $("#messages").scrollTop = $("#messages").scrollHeight;
  } catch (error) {
    renderMessage(
      "assistant",
      `Could not load the mock answer: ${error.message}`,
    );
  } finally {
    $("#chat-submit").disabled = false;
    $("#chat-submit").textContent = "Send ↗";
    $("#messages").setAttribute("aria-busy", "false");
    input.focus();
  }
}

async function submitMetric(event) {
  event.preventDefault();
  const submit = $("#metric-submit");
  submit.disabled = true;
  submit.textContent = "Mapping relationships…";
  try {
    const request = Object.fromEntries(new FormData(event.target));
    const result = await api.addMetric(request);
    const edges = [...result.accepted, ...result.ambiguous, ...result.rejected];
    state.newEdges = new Set(edges.map((edge) => edge.id));
    if (mockMode) applyMockMetric(result.metric, edges);
    else await refreshGraph();
    $("#metric-result").innerHTML =
      `<strong>${mockMode ? "Mock metric" : "Metric"} mapped: ${escapeHtml(result.metric.name)}</strong><p>${escapeHtml(result.notes)}</p>${["accepted", "ambiguous", "rejected"].map((group) => `<h4>${readable(group)} (${result[group].length})</h4><ul>${result[group].map((edge) => `<li>${escapeHtml(edge.src)} → ${escapeHtml(edge.dst)} · ${escapeHtml(readable(edge.type))}</li>`).join("") || "<li>None</li>"}</ul>`).join("")}<p>New relationships are highlighted in the graph.</p><details><summary>Full AddMetricResult</summary><pre>${escapeHtml(JSON.stringify(result, null, 2))}</pre></details>`;
  } catch (error) {
    $("#metric-result").textContent = error.message;
  } finally {
    submit.disabled = false;
    submit.textContent = "Ask Cartographer";
  }
}

function applyMockMetric(metric, edges) {
  if (!state.metrics.some((item) => item.name === metric.name))
    state.metrics.push(metric);
  const ids = new Set(state.graph.nodes.map((node) => node.id));
  for (const id of new Set([
    metric.name,
    ...edges.flatMap((edge) => [edge.src, edge.dst]),
  ])) {
    if (ids.has(id)) continue;
    const definition = state.metrics.find((item) => item.name === id);
    state.graph.nodes.push({
      id,
      label: readable(id),
      domain: definition?.domain,
      unit: definition?.unit,
      state: "normal",
    });
  }
  for (const edge of edges)
    if (!state.graph.edges.some((item) => item.id === edge.id))
      state.graph.edges.push(edge);
  renderGraph();
  renderIncidents();
}

function bindControls() {
  $("#metric-select").addEventListener("change", (event) =>
    attempt(() => selectMetric(event.target.value)),
  );
  $("#show-rejected").addEventListener("change", setRejectedVisibility);
  $("#fit-graph").addEventListener("click", () => state.cy?.fit(undefined, 30));
  $("#clock-toggle").addEventListener("click", () =>
    attempt(async () => {
      if (!state.status) return;
      updateStatus(await (state.status.paused
        ? api.resumeClock()
        : api.pauseClock()));
    }),
  );
  $("#clock-step").addEventListener("click", () =>
    attempt(async () => {
      updateStatus(await api.stepClock());
      if (mockMode) appendEvent({
        type: "tick",
        sim_ts: state.status.sim_ts,
        payload: { tick: state.status.tick },
      });
    }),
  );
  $("#scenarios").addEventListener("click", (event) =>
    attempt(async () => {
      const button = event.target.closest("[data-scenario]");
      if (!button || button.disabled) return;
      const active = button.getAttribute("aria-pressed") === "true";
      const scenario = await (active
        ? api.clearScenario(button.dataset.scenario)
        : api.injectScenario(button.dataset.scenario));
      renderScenario(scenario);
      notify(
        `${scenario.id} ${scenario.active ? "injected" : "cleared"}${mockMode ? " locally. Mock incident and chart fixtures remain unchanged." : "."}`,
      );
    }),
  );
  $(".tabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-status]");
    if (!button) return;
    state.filter = button.dataset.status;
    state.openingIncident = null;
    state.incident = null;
    document
      .querySelectorAll("[data-status]")
      .forEach((tab) =>
        tab.setAttribute("aria-pressed", String(tab === button)),
      );
    renderIncidents();
    renderIncidentDetail();
    if (filteredIncidents()[0])
      attempt(() => openIncident(filteredIncidents()[0].id));
  });
  $("#incidents-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-incident]");
    if (button) attempt(() => openIncident(button.dataset.incident));
  });
  $("#messages").addEventListener("click", (event) =>
    attempt(async () => {
      const button = event.target.closest("[data-chat-incident]");
      if (!button) return;
      state.filter = "open";
      document
        .querySelectorAll("[data-status]")
        .forEach((tab) =>
          tab.setAttribute(
            "aria-pressed",
            String(tab.dataset.status === "open"),
          ),
        );
      await openIncident(button.dataset.chatIncident, true);
    }),
  );
  $("#chat-suggestion").addEventListener("click", () => {
    $("#chat-input").value = "Why did orders drop?";
    $("#chat-form").requestSubmit();
  });
  $("#chat-form").addEventListener("submit", sendChat);
  $("#add-metric").addEventListener("click", () =>
    $("#metric-dialog").showModal(),
  );
  $("#close-dialog").addEventListener("click", () =>
    $("#metric-dialog").close(),
  );
  $("#metric-form").addEventListener("submit", submitMetric);
}

async function init() {
  $("#mode-label").textContent = mockMode ? "MOCK DATA · NO LIVE CALLS" : "LIVE API";
  bindControls();
  if (mockMode) {
    $("#feed-caption").textContent = "Replay · one event every 2 seconds · last 30";
    $("#feed").setAttribute("aria-label", "Replayed events");
  } else {
    $(".mock-callout").hidden = true;
    connectEvents();
    await refreshLive();
    return;
  }
  const [status, graph, metrics, incidents, scenarios, events] =
    await Promise.all([
      api.getStatus(),
      api.getGraph(),
      api.getMetrics(),
      api.getIncidents(),
      api.getScenarios(),
      api.getEvents(),
    ]);
  Object.assign(state, { status, graph, metrics, incidents, events });
  renderStatus();
  renderScenarios(scenarios);
  renderGraph();
  renderIncidents();
  const first = filteredIncidents()[0];
  await Promise.all([
    state.graph.nodes.some((node) => node.id === "orders")
      ? selectMetric("orders")
      : state.graph.nodes[0] && selectMetric(state.graph.nodes[0].id),
    first && openIncident(first.id),
  ]);
  replayNext();
  setInterval(() => {
    if (!state.status.paused) replayNext();
  }, 2000);
}

attempt(init);
