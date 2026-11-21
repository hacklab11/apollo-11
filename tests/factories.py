"""Deterministic examples for contract tests and the mock UI."""

from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict

from eye.contracts import (
    Actor,
    AddMetricResult,
    Anomaly,
    Contribution,
    Dimension,
    Edge,
    EdgeType,
    Event,
    Evidence,
    GraphView,
    Hypothesis,
    Incident,
    MetricDef,
    ScenarioId,
    ScenarioState,
    SentinelDecision,
    Series,
    SeriesPoint,
    StepAction,
    TrailStep,
)

CAMPAIGN_TS = datetime(2026, 11, 27, tzinfo=UTC)
S7_TS = CAMPAIGN_TS + timedelta(hours=3)
S6_TS = CAMPAIGN_TS + timedelta(hours=13)
MOCK_TS = CAMPAIGN_TS + timedelta(hours=14)
REAL_TS = datetime(2026, 11, 21, 12, tzinfo=UTC)


class AgentHealth(TypedDict):
    ok: bool
    url: str


class StatusResponse(TypedDict):
    sim_ts: datetime
    tick: int
    running: bool
    paused: bool
    llm_mode: Literal["live", "record", "replay"]
    llm_calls: int
    tokens_in: int
    tokens_out: int
    agents: dict[str, AgentHealth]
    active_scenarios: list[ScenarioId]
    investigations_running: int


class MetricDefinitionResponse(MetricDef):
    in_edges: list[Edge]
    out_edges: list[Edge]


class ChatToolCall(TypedDict):
    tool: str
    input: dict[str, object]
    summary: str


class ChatResponse(TypedDict):
    answer: str
    tool_calls: list[ChatToolCall]
    incident_ids: list[str]


class TruthRow(TypedDict):
    id: str
    ts: datetime
    scenario_id: ScenarioId
    action: Literal["inject", "clear"]
    params: dict[str, object]


def make_orders_series() -> Series:
    points = []
    for offset in range(-47, 1):
        ts = MOCK_TS + timedelta(hours=offset)
        baseline = 1000 + 15 * abs(ts.hour - S7_TS.hour)
        expected = round(baseline * (1.35 if ts >= CAMPAIGN_TS else 1))
        value = (
            round(expected * 0.6) if ts >= S7_TS else expected + (offset % 5 - 2) * 7
        )
        points.append(
            SeriesPoint(
                ts=ts,
                value=value,
                expected=expected,
                lower=round(expected * 0.95, 2),
                upper=round(expected * 1.05, 2),
            )
        )
    return Series(metric="orders", segment={}, points=points)


def make_anomaly() -> Anomaly:
    return Anomaly(
        id="anm_s7",
        ts=S7_TS,
        metric="orders",
        segment={},
        kind="point",
        actual=810,
        expected=1350,
        z=-9.0,
        delta=-540,
        delta_pct=-40,
        window_hours=1,
        impact_eur_per_hour=97200,
        completeness=1.0,
        priority=0.99,
    )


def make_incident_s7() -> Incident:
    entry = make_anomaly()
    root = Hypothesis(
        id="hyp_s7_release",
        statement="Checkout-web v5.8 broke Place Order on web and Android; native iOS was unaffected.",
        cause_type="release",
        root_node="cart_to_checkout_rate",
        segment={"platform": "web"},
        first_change_ts=S7_TS,
        explained_share=0.9,
        status="supported",
        evidence_for=[
            Evidence(
                group="data",
                source="drill_down",
                summary="Cart-to-checkout rate fell 85% on web and Android; iOS changed by 1%.",
                numbers={"affected_drop_pct": 85, "ios_drop_pct": 1},
                step_id="stp_s7_03",
            ),
            Evidence(
                group="change_log",
                source="list_events",
                summary="Checkout-web v5.8 shipped at 03:00 UTC, the first hour of the drop.",
                numbers={"version": "v5.8"},
                step_id="stp_s7_04",
            ),
            Evidence(
                group="engineering",
                source="discovery",
                summary="TypeError affected 80% of carts on the bundle shared by web and Android.",
                numbers={"affected_carts_pct": 80},
                step_id="stp_s7_05",
            ),
        ],
    )
    psp = Hypothesis(
        id="hyp_s7_psp",
        statement="PSP-2 latency caused the checkout outage.",
        cause_type="unknown",
        root_node="payment_success_rate",
        segment={"psp": "PSP-2"},
        first_change_ts=S7_TS - timedelta(hours=1),
        explained_share=0,
        status="refuted",
        evidence_against=[
            Evidence(
                group="data",
                source="query_metric",
                summary="PSP-2 still approved 98% of attempts; the missing attempts never reached it.",
                numbers={"success_rate": 0.98},
                step_id="stp_s7_06",
            ),
        ],
    )
    app = Hypothesis(
        id="hyp_s7_android",
        statement="The v9.14 Android rollout broke the native app.",
        cause_type="release",
        root_node="cart_to_checkout_rate",
        segment={"platform": "android"},
        first_change_ts=CAMPAIGN_TS,
        explained_share=0,
        status="refuted",
        evidence_against=[
            Evidence(
                group="data",
                source="drill_down",
                summary="Checkout fell 85% on web too, where the Android release does not run.",
                numbers={"web_drop_pct": 85},
                step_id="stp_s7_03",
            ),
        ],
    )
    return Incident(
        id="inc_s7",
        fingerprint="release|cart_to_checkout_rate|platform=web",
        title="Black Friday checkout release broke Place Order",
        status="open",
        verdict="CONFIRMED",
        opened_ts=entry.ts,
        updated_ts=entry.ts,
        last_seen_ts=entry.ts,
        entry=entry,
        summary=(
            "Checkout-web v5.8 broke Place Order on web and Android; native iOS was unaffected. "
            "Orders fell 40% below the Black Friday forecast, costing €97,200 in GMV per hour. "
            "PSP-2's approval rate held steady, so its latency warning does not explain the drop."
        ),
        impact_eur_per_hour=entry.impact_eur_per_hour,
        impact_eur_total=entry.impact_eur_per_hour,
        affected_actors=["web", "android"],
        contributions=[
            Contribution(
                factor="cart_to_checkout_rate", kind="rate", delta=-486, share=0.9
            ),
            Contribution(factor="unexplained", kind="residual", delta=-54, share=0.1),
        ],
        root_cause=root,
        hypotheses=[root, psp, app],
        investigation_ids=["inv_s7"],
        open_questions=[
            "How quickly will Android clients pick up the reverted bundle?"
        ],
        suggested_actions=[
            "Roll back checkout-web to v5.7 and verify web and Android checkout."
        ],
    )


def _make_s6_anomaly() -> Anomaly:
    return Anomaly(
        id="anm_s6",
        ts=S6_TS,
        metric="small_seller_gmv_share",
        segment={"category": "electronics"},
        kind="divergent_segment",
        actual=0.18,
        expected=0.30,
        z=-4.4,
        delta=-0.12,
        delta_pct=-40,
        window_hours=6,
        share_now=0.18,
        share_expected=0.30,
        total_z=0.9,
        impact_eur_per_hour=4200,
        completeness=1.0,
        priority=0.85,
    )


def make_incident_s6() -> Incident:
    entry = _make_s6_anomaly()
    root = Hypothesis(
        id="hyp_s6_price",
        statement="A 15% 1P price cut triggered a repricer loop, then small sellers stocked out.",
        cause_type="price_change",
        root_node="price_index_vs_1p",
        segment={"category": "electronics"},
        first_change_ts=CAMPAIGN_TS + timedelta(hours=2),
        explained_share=0.875,
        status="supported",
        evidence_for=[
            Evidence(
                group="external",
                source="selleros",
                summary="SellerOS confirmed 3 small sellers had no repricer floor.",
                numbers={"sellers_without_floor": 3},
                step_id="stp_s6_01",
            ),
        ],
    )
    return Incident(
        id="inc_s6",
        fingerprint="price_change|price_index_vs_1p|category=electronics",
        title="Black Friday repricer loop squeezed small electronics sellers",
        status="open",
        verdict="MIXED",
        opened_ts=entry.ts,
        updated_ts=entry.ts,
        last_seen_ts=entry.ts,
        entry=entry,
        summary=(
            "A 15% 1P price cut set off a repricer loop: lower prices won the Buy Box, "
            "then small sellers sold out and bids rose. Small-seller GMV share fell from "
            "30% to 18% despite planned Black Friday demand. S-99 took 3 Buy Boxes; "
            "SellerOS confirmed 3 sellers had no price floor and was notified."
        ),
        impact_eur_per_hour=entry.impact_eur_per_hour,
        impact_eur_total=entry.impact_eur_per_hour,
        affected_actors=["S-03", "S-07", "S-12"],
        contributions=[
            Contribution(
                factor="repricer loop", kind="rate", delta=-0.105, share=0.875
            ),
            Contribution(factor="stockouts", kind="mix", delta=-0.035, share=0.292),
            Contribution(
                factor="Black Friday campaign", kind="planned", delta=0.02, share=-0.167
            ),
        ],
        root_cause=root,
        hypotheses=[root],
        investigation_ids=["inv_s6"],
        open_questions=[
            "Are small sellers at risk of churn?",
            "Will S-99 refunds rise?",
        ],
        suggested_actions=[
            "Set repricer floors and prioritize restock capacity for affected sellers."
        ],
        notifications=[
            {
                "to": "selleros",
                "ts": entry.ts.isoformat(),
                "ok": True,
                "task_id": "task_mock_s6",
            }
        ],
    )


def make_trail_s7() -> list[TrailStep]:
    steps: list[tuple[Actor, StepAction, str | None, str, str]] = [
        (
            "sentinel",
            "decision",
            None,
            "Orders missed the forecast by 40%.",
            "High GMV impact.",
        ),
        (
            "detective",
            "tool_call",
            "check_freshness",
            "Orders data are 100% complete.",
            "Rule out a data gap.",
        ),
        (
            "detective",
            "tool_call",
            "drill_down",
            "Web and Android cart-to-checkout fell 85%; iOS changed by 1%.",
            "Find the first broken step and compare platforms.",
        ),
        (
            "detective",
            "tool_call",
            "list_events",
            "Checkout-web v5.8 shipped at 03:00 UTC.",
            "Check changes at the first hour of the drop.",
        ),
        (
            "discovery",
            "a2a_call",
            "ask_specialist",
            "Checkout TypeError affected 80% of carts on the shared web and Android bundle.",
            "Verify which platforms use the new component.",
        ),
        (
            "detective",
            "tool_call",
            "query_metric",
            "PSP-2 approved 98% of attempts; the number of attempts fell.",
            "Test the payment-provider explanation.",
        ),
        (
            "skeptic",
            "tool_call",
            "planned_effect_check",
            "The campaign forecast called for 35% more orders, not a fall.",
            "Compare against planned Black Friday demand.",
        ),
        (
            "skeptic",
            "decision",
            None,
            "The Android-only v9.14 rollout cannot explain the 85% web drop.",
            "Refute the platform-specific explanation.",
        ),
        (
            "detective",
            "decision",
            None,
            "Checkout-web explains 90% of the missing orders.",
            "Funnel, release and client errors support the same cause.",
        ),
        (
            "reporter",
            "decision",
            None,
            "Opened inc_s7: €97,200 GMV lost per hour.",
            "Recommend rolling back checkout-web to v5.7.",
        ),
    ]
    return [
        TrailStep(
            id=f"stp_s7_{n:02d}",
            investigation_id="inv_s7",
            n=n,
            real_ts=REAL_TS + timedelta(seconds=n),
            actor=actor,
            action=action,
            tool=tool,
            reason=reason,
            output_summary=summary,
        )
        for n, (actor, action, tool, summary, reason) in enumerate(steps, 1)
    ]


def make_metrics() -> list[MetricDef]:
    return [
        MetricDef(
            name="orders",
            description="Units ordered per hour.",
            unit="count",
            domain="orders",
            kind="sql",
            dimensions=["country", "platform", "category", "seller_tier", "channel"],
            higher_is_better=True,
            impact_weight=1.0,
        ),
        MetricDef(
            name="sessions",
            description="Marketplace sessions per hour.",
            unit="count",
            domain="traffic",
            kind="sql",
            dimensions=["country", "platform"],
        ),
        MetricDef(
            name="carts",
            description="Carts started per hour.",
            unit="count",
            domain="orders",
            kind="sql",
            dimensions=["country", "platform"],
        ),
        MetricDef(
            name="cart_to_checkout_rate",
            description="Share of carts that start checkout.",
            unit="ratio",
            domain="orders",
            kind="sql",
            dimensions=["country", "platform"],
        ),
        MetricDef(
            name="checkout_conversion",
            description="Orders per cart.",
            unit="ratio",
            domain="orders",
            kind="formula",
            expression="orders / carts",
            dimensions=["country", "platform"],
        ),
        MetricDef(
            name="payment_success_rate",
            description="Share of approved payment attempts.",
            unit="ratio",
            domain="quality",
            kind="sql",
            dimensions=["country", "platform", "psp"],
        ),
        MetricDef(
            name="client_error_rate",
            description="Client errors per session.",
            unit="ratio",
            domain="quality",
            kind="sql",
            dimensions=["platform"],
            higher_is_better=False,
        ),
        MetricDef(
            name="small_seller_gmv_share",
            description="Share of GMV from small sellers.",
            unit="ratio",
            domain="marketplace",
            kind="sql",
            dimensions=["country", "category"],
        ),
        MetricDef(
            name="price_index_vs_1p",
            description="Relative Buy Box price index versus 1P.",
            unit="index",
            domain="marketplace",
            kind="sql",
            dimensions=["category"],
        ),
        MetricDef(
            name="buybox_share",
            description="Share of Buy Box GMV by seller tier.",
            unit="ratio",
            domain="marketplace",
            kind="sql",
            dimensions=["category", "seller_tier"],
        ),
        MetricDef(
            name="in_stock_rate",
            description="Share of offers in stock.",
            unit="ratio",
            domain="supply",
            kind="sql",
            dimensions=["category", "seller_tier"],
        ),
        MetricDef(
            name="avg_cpc",
            description="Average advertising cost per click.",
            unit="eur",
            domain="ads",
            kind="sql",
            dimensions=["country", "category", "advertiser_id"],
        ),
    ]


def _edge(
    src: str,
    dst: str,
    kind: EdgeType,
    *,
    identity: str | None = None,
    dimension: Dimension | None = None,
    lag: int | None = None,
) -> Edge:
    return Edge(
        id=f"edg_{src}_{dst}" + (f"_{dimension}" if dimension else ""),
        src=src,
        dst=dst,
        type=kind,
        status="hypothesis" if kind == "causal_hypothesis" else "validated",
        identity=identity,
        dimension=dimension,
        lag_hours=lag,
        confidence=0.8 if kind == "causal_hypothesis" else 1.0,
        evidence={},
        created_by="bootstrap",
    )


def make_graph() -> GraphView:
    states = {
        "orders": "incident",
        "small_seller_gmv_share": "incident",
        "cart_to_checkout_rate": "anomaly",
        "client_error_rate": "anomaly",
        "avg_cpc": "watch",
    }
    checkout_identity = "checkout_conversion = cart_to_checkout_rate * checkout_to_payment_rate * payment_success_rate"
    return GraphView(
        nodes=[
            {
                "id": metric.name,
                "label": metric.name.replace("_", " ").capitalize(),
                "domain": metric.domain,
                "unit": metric.unit,
                "state": states.get(metric.name, "normal"),
                "impact_weight": metric.impact_weight,
            }
            for metric in make_metrics()
        ],
        edges=[
            _edge(
                "sessions", "carts", "formula", identity="carts = sessions * cart_rate"
            ),
            _edge(
                "cart_to_checkout_rate",
                "checkout_conversion",
                "formula",
                identity=checkout_identity,
            ),
            _edge(
                "payment_success_rate",
                "checkout_conversion",
                "formula",
                identity=checkout_identity,
            ),
            _edge(
                "carts",
                "orders",
                "formula",
                identity="orders = carts * checkout_conversion",
            ),
            _edge(
                "checkout_conversion",
                "orders",
                "formula",
                identity="orders = carts * checkout_conversion",
            ),
            _edge("cart_to_checkout_rate", "orders", "causal_hypothesis", lag=0),
            _edge(
                "client_error_rate", "cart_to_checkout_rate", "causal_hypothesis", lag=0
            ),
            _edge("price_index_vs_1p", "buybox_share", "causal_hypothesis", lag=1),
            _edge("buybox_share", "in_stock_rate", "causal_hypothesis", lag=6),
            _edge("in_stock_rate", "price_index_vs_1p", "causal_hypothesis", lag=1),
            _edge("in_stock_rate", "avg_cpc", "causal_hypothesis", lag=2),
            _edge("buybox_share", "small_seller_gmv_share", "causal_hypothesis", lag=0),
            *[
                _edge("orders", "orders", "breakdown", dimension=dim)
                for dim in ("country", "platform", "category", "seller_tier", "channel")
            ],
        ],
    )


def make_orders_definition() -> MetricDefinitionResponse:
    orders = next(metric for metric in make_metrics() if metric.name == "orders")
    edges = make_graph().edges
    return MetricDefinitionResponse(
        **orders.model_dump(exclude={"sql"}),
        sql=(
            "SELECT ts, country, platform, category, seller_tier, channel, "
            "SUM(units) AS num FROM orders_hourly GROUP BY ALL"
        ),
        in_edges=[edge for edge in edges if edge.dst == orders.name],
        out_edges=[edge for edge in edges if edge.src == orders.name],
    )


def make_add_metric_result() -> AddMetricResult:
    metric = MetricDef(
        name="orders_per_session",
        description="Orders divided by marketplace sessions.",
        unit="ratio",
        domain="orders",
        kind="formula",
        expression="orders / sessions",
        dimensions=["country", "platform"],
        added_by="user",
    )
    identity = "orders_per_session = orders / sessions"
    return AddMetricResult(
        metric=metric,
        accepted=[
            _edge(src, metric.name, "formula", identity=identity).model_copy(
                update={"created_by": "cartographer"}
            )
            for src in ("orders", "sessions")
        ],
        ambiguous=[],
        rejected=[],
        notes="Validated both formula inputs at the total, country and platform levels.",
        trail_investigation_id="inv_add_orders_per_session",
    )


def make_scenarios() -> list[ScenarioState]:
    titles: dict[ScenarioId, str] = {
        "S1": "Ads pacing bug",
        "S2": "Category sales fall, demand stable",
        "S3": "German search regression",
        "S4": "Feed relevance regression",
        "S5": "Orders up, contribution down",
        "S6": "Black Friday cascade",
        "S7": "Black Friday checkout outage",
        "I1": "Planned Black Friday campaign",
        "I2": "French data arrived late",
    }
    injected = {"I1": CAMPAIGN_TS, "S6": CAMPAIGN_TS, "S7": S7_TS}
    return [
        ScenarioState(
            id=id_,
            title=title,
            active=id_ in injected,
            injected_ts=injected.get(id_),
            cleared_ts=None,
        )
        for id_, title in titles.items()
    ]


def make_status() -> StatusResponse:
    return {
        "sim_ts": MOCK_TS,
        "tick": 14,
        "running": True,
        "paused": False,
        "llm_mode": "replay",
        "llm_calls": 12,
        "tokens_in": 29800,
        "tokens_out": 3100,
        "agents": {
            "growth": {"ok": True, "url": "http://localhost:8200/growth"},
            "marketplace": {"ok": True, "url": "http://localhost:8200/marketplace"},
            "discovery": {"ok": True, "url": "http://localhost:8200/discovery"},
            "selleros": {"ok": True, "url": "http://localhost:8300"},
        },
        "active_scenarios": [
            scenario.id for scenario in make_scenarios() if scenario.active
        ],
        "investigations_running": 0,
    }


def make_chat_response() -> ChatResponse:
    return {
        "answer": (
            "**inc_s7:** Orders came in 40% below the Black Friday forecast after checkout-web v5.8 "
            "broke Place Order on web and Android. iOS was unaffected; roll back to v5.7."
        ),
        "tool_calls": [
            {
                "tool": "get_incident",
                "input": {"id": "inc_s7"},
                "summary": "€97,200 GMV lost per hour.",
            }
        ],
        "incident_ids": ["inc_s7"],
    }


def make_truth() -> list[TruthRow]:
    return [
        {
            "id": "scn_i1",
            "ts": CAMPAIGN_TS,
            "scenario_id": "I1",
            "action": "inject",
            "params": {"campaign_mult": 1.4},
        },
        {
            "id": "scn_s6",
            "ts": CAMPAIGN_TS,
            "scenario_id": "S6",
            "action": "inject",
            "params": {"price_cut_pct": 15},
        },
        {
            "id": "scn_s7",
            "ts": S7_TS,
            "scenario_id": "S7",
            "action": "inject",
            "params": {"checkout_ok": {"web": 0.1, "android": 0.1}},
        },
    ]


def make_decisions() -> list[SentinelDecision]:
    return [
        SentinelDecision(
            anomaly=make_anomaly(),
            action="investigate",
            reason="Orders missed the forecast by 40%.",
        ),
        SentinelDecision(
            anomaly=_make_s6_anomaly(),
            action="investigate",
            reason="Small-seller GMV share fell 12 percentage points in electronics.",
        ),
    ]


def make_incident_list() -> list[Incident]:
    return [
        incident.model_copy(update={"hypotheses": [], "root_cause": None})
        for incident in (make_incident_s7(), make_incident_s6())
    ]


def make_events() -> list[Event]:
    s7, s6 = make_incident_s7(), make_incident_s6()
    decision_s7, decision_s6 = make_decisions()
    selleros = TrailStep(
        id="stp_s6_01",
        investigation_id="inv_s6",
        n=1,
        real_ts=REAL_TS + timedelta(seconds=30),
        actor="selleros",
        action="a2a_call",
        tool="repricer_status",
        output_summary="SellerOS confirmed S-03, S-07 and S-12 had no repricer floor.",
        reason="Check whether small sellers priced below cost.",
    )
    return [
        Event(type="tick", sim_ts=S7_TS, payload={"tick": 3}),
        Event(type="anomaly", sim_ts=S7_TS, payload=s7.entry.model_dump(mode="json")),
        Event(
            type="sentinel_decision",
            sim_ts=S7_TS,
            payload=decision_s7.model_dump(mode="json"),
        ),
        *[
            Event(type="trail_step", sim_ts=S7_TS, payload=step.model_dump(mode="json"))
            for step in make_trail_s7()
        ],
        Event(
            type="incident_upserted", sim_ts=S7_TS, payload=s7.model_dump(mode="json")
        ),
        Event(type="tick", sim_ts=S6_TS, payload={"tick": 13}),
        Event(type="anomaly", sim_ts=S6_TS, payload=s6.entry.model_dump(mode="json")),
        Event(
            type="sentinel_decision",
            sim_ts=S6_TS,
            payload=decision_s6.model_dump(mode="json"),
        ),
        Event(
            type="trail_step", sim_ts=S6_TS, payload=selleros.model_dump(mode="json")
        ),
        Event(
            type="incident_upserted", sim_ts=S6_TS, payload=s6.model_dump(mode="json")
        ),
        Event(type="tick", sim_ts=MOCK_TS, payload={"tick": 14}),
    ]
