"""Scenario lifecycle and hook-based marketplace interventions."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from eye.contracts import ScenarioId, ScenarioState
from eye.sim.changelog import add_change, add_external_status
from eye.sim.engine import Hour, Simulator
from eye.sim.store import Batches

_TITLES = {
    "S1": "Ad budget spent by 10:00",
    "S2": "Category sales fall, demand stable",
    "S3": "German search relevance regression",
    "S4": "Feed clicks up, purchases down",
    "S5": "Orders up, contribution down",
    "S7": "Black Friday checkout outage",
    "I1": "Planned Black Friday campaign",
    "I2": "Late FR order partitions",
}


class ScenarioManager:
    def __init__(
        self, sim: Simulator, *, config_path: str | Path = "config/scenarios.yaml"
    ) -> None:
        self.sim = sim
        self.params: dict[str, dict[str, Any]] = yaml.safe_load(
            Path(config_path).read_text(encoding="utf-8")
        )
        self._states: list[ScenarioState] = []
        self._pending: dict[ScenarioId, datetime] = {}
        self._late: list[tuple[datetime, datetime, list[tuple]]] = []
        self._discounts = sim.finance.platform_discount.copy()
        self._checkout_version = sim.apps.versions["checkout-web"]
        self._pacing_exempt = sim.ads.pacing_exempt.copy()
        self._cpc_mult = sim.ads.cpc_mult.copy()
        self._impression_mult = sim.ads.impression_mult.copy()
        self._ads_version = sim.ads.version
        self._po_mult = sim.market.po_quantity_mult.copy()
        self._po_version = sim.market.po_version.copy()
        self._replenishment_mult = sim.market.replenishment_mult.copy()
        sim.pre_demand_hooks.append(self._before)
        sim.pre_store_hooks.append(self._before_store)

    def inject(self, id: ScenarioId, at_ts: datetime | None = None) -> ScenarioState:
        if id not in _TITLES:
            raise ValueError(f"unsupported scenario: {id}")
        if any(s.id == id and s.cleared_ts is None for s in self._states):
            raise ValueError(f"scenario already injected: {id}")
        ts = self.sim.now if at_ts is None else at_ts
        if (
            ts.tzinfo is None
            or ts < self.sim.now
            or ts.minute
            or ts.second
            or ts.microsecond
        ):
            raise ValueError("injection requires an aware current or future whole hour")
        state = ScenarioState(
            id=id, title=_TITLES[id], active=False, injected_ts=ts, cleared_ts=None
        )
        self._states.append(state)
        self._pending[id] = ts
        if ts == self.sim.now:
            self._start(state)
        return state.model_copy(deep=True)

    def clear(self, id: ScenarioId) -> None:
        state = next(
            (s for s in reversed(self._states) if s.id == id and s.cleared_ts is None),
            None,
        )
        if state is None:
            raise ValueError(f"scenario not injected: {id}")
        state.active = False
        state.cleared_ts = self.sim.now
        self._pending.pop(id, None)
        self._log(id, "clear")
        if id == "S7":
            self.sim.apps.release("checkout-web", self._checkout_version, self.sim.now)
        self._reset_switches()

    def active(self) -> list[ScenarioState]:
        return [s.model_copy(deep=True) for s in self._states if s.active]

    def timeline(self) -> list[ScenarioState]:
        return [s.model_copy(deep=True) for s in self._states]

    def _log(self, id: ScenarioId, action: str) -> None:
        self.sim.store.append(
            {
                "scenario_log": [
                    (
                        f"scenario-{len(self._states)}-{id}-{action}-{self.sim.now.isoformat()}",
                        self.sim.now,
                        id,
                        action,
                        json.dumps(self.params[id]),
                    )
                ]
            }
        )

    def _start(self, state: ScenarioState) -> None:
        state.active = True
        self._pending.pop(state.id)
        self._log(state.id, "inject")
        self._changes(state.id)

    def _changes(self, id: ScenarioId) -> None:
        sim, ts, p = self.sim, self.sim.now, self.params[id]
        batches: Batches = {}

        def change(system: str, description: str, **kwargs: Any) -> None:
            add_change(
                batches, ts, system, description=description, planned=False, **kwargs
            )

        if id == "S1":
            change(
                "pacing-svc",
                "pacing-svc v4.1: new budget smoothing",
                scope={"advertisers": "all"},
            )
        elif id == "S2":
            release = ts - timedelta(days=p["release_days_ago"])
            add_change(
                batches,
                release,
                "purchasing",
                planned=False,
                description="purchasing v2.3: new demand forecast model",
            )
            with sim.store.cursor() as con:
                con.execute(
                    """UPDATE purchase_orders SET qty = round(qty * ?),
                    delivered_qty = round(delivered_qty * ?), system_version = 'purchasing v2.3'
                    WHERE sku LIKE 'H%' AND ordered_ts >= ?""",
                    [p["po_mult"], p["po_mult"], release],
                )
            sim.market.pos = [
                (
                    *po[:4],
                    round(po[4] * p["po_mult"]),
                    *po[5:7],
                    round(po[7] * p["po_mult"]),
                    "purchasing v2.3",
                )
                if po[1].startswith("H") and po[3] >= release
                else po
                for po in sim.market.pos
            ]
            for sku in (f"H{i:02d}" for i in range(1, 7)):
                for offer in sim.world.offers[sku]:
                    offer.stock = round(
                        sim.market.average_sales(offer, ts) * p["stock_days"]
                    )
            # T05 has no first-party offer in the catalog; supplier history is independent.
            batches["purchase_orders"] = [
                (
                    f"PO-decoy-{ts.isoformat()}",
                    "T05",
                    sim.world.skus["T05"].supplier_id,
                    ts - timedelta(hours=48),
                    10,
                    ts,
                    ts + timedelta(hours=p["delay_hours"]),
                    10,
                    "purchasing v2.2",
                )
            ]
        elif id == "S3":
            change("search-ranker", "search-ranker v7.2", scope={"country": "DE"})
            change("search-ui", "search-ui v3.0", scope={"country": "FR"})
            add_change(
                batches,
                ts - timedelta(hours=2),
                "app-android",
                description="app-android routine release",
            )
        elif id == "S4":
            change(
                "foryou-reco",
                "foryou model v12 (engagement objective)",
                kind="model_deploy",
            )
            change("catalog-svc", "catalog-svc routine release")
        elif id == "S5":
            add_change(
                batches,
                ts,
                "growth",
                kind="campaign",
                planned=True,
                description="paid social push +200% (Cyber Week)",
                expected_effect={"sessions": "+20%"},
            )
            change(
                "promo-engine",
                "deals config 2026-11: platform funds 20% on home deals",
                kind="config",
            )
        elif id == "I1":
            add_change(
                batches,
                ts,
                "growth",
                kind="campaign",
                planned=True,
                description="black-friday-2026",
                expected_effect={
                    "sessions": "+40%",
                    "orders": "+35%",
                    "platform_discounts_eur_per_day": 25000,
                },
            )
        elif id == "S7":
            change(
                "checkout-web", "checkout-web v5.8: new Place Order button component"
            )
            sim.apps.release("checkout-web", "checkout-web v5.8", ts)
            sim.apps.release("android", "app-android v9.14", ts.replace(hour=0))
            add_change(
                batches,
                ts.replace(hour=0),
                "app-android",
                description="app-android v9.14",
            )
            add_external_status(
                batches,
                ts - timedelta(hours=1),
                "PSP-2",
                "degraded",
                "elevated latency",
            )
            with sim.store.cursor() as con:
                con.execute(
                    "UPDATE payments_hourly SET latency_p95_ms = ? WHERE ts = ? AND psp = 'PSP-2'",
                    [p["psp_latency_ms"], ts - timedelta(hours=1)],
                )
        for table in ("change_log", "external_status"):
            if table in batches:
                batches[table] = [
                    (f"{r[0]}-{id}-{len(self._states)}", *r[1:]) for r in batches[table]
                ]
        self.sim.store.append(batches)

    def _reset_switches(self) -> None:
        sim = self.sim
        sim.ads.pacing_exempt = self._pacing_exempt.copy()
        sim.ads.cpc_mult = self._cpc_mult.copy()
        sim.ads.impression_mult = self._impression_mult.copy()
        sim.ads.version = self._ads_version
        sim.market.po_quantity_mult = self._po_mult.copy()
        sim.market.po_version = self._po_version.copy()
        sim.market.replenishment_mult = self._replenishment_mult.copy()
        sim.finance.platform_discount = self._discounts.copy()

    def _before(self, sim: Simulator, hour: Hour) -> None:
        for state in self._states:
            if state.id in self._pending and self._pending[state.id] <= hour.ts:
                self._start(state)
        self._reset_switches()
        for state in self.active():
            id, p = state.id, self.params[state.id]
            if id == "S1":
                sim.ads.pacing_exempt.add(p["advertiser"])
                sim.ads.cpc_mult["electronics"] = p["cpc_mult"]
                sim.ads.impression_mult[p["advertiser"]] = p["impression_mult"]
                sim.ads.version = "pacing-svc v4.1"
            elif id == "S2":
                for sku in sim.world.skus:
                    if sku.startswith("H"):
                        sim.market.po_quantity_mult[sku] = p["po_mult"]
                        sim.market.po_version[sku] = "purchasing v2.3"
                        sim.market.replenishment_mult[sku] = p["po_mult"]
            elif id == "S3":
                hour.demand.relevance["DE"] = p["relevance"]
                hour.demand.isolate_relevance = True
            elif id == "S4":
                hour.demand.exposure_power = p["exposure_power"]
                hour.demand.unavailable_weight = p["unavailable_weight"]
            elif id == "S5":
                hour.demand.marketing_mult = dict.fromkeys(
                    ("DE", "FR"), p["session_mult"]
                )
                hour.demand.marketing_spend_mult = dict.fromkeys(
                    ("DE", "FR"), p["spend_mult"]
                )
                hour.demand.extra_session_conversion = p["extra_conversion"]
                hour.demand.exposure_mult.update(
                    {
                        s.sku: p["home_exposure_mult"]
                        for s in sim.world.skus.values()
                        if s.category == "home"
                    }
                )
            elif id == "I1":
                hour.demand.coupled_volume_mult *= p["campaign_mult"]
            elif id == "S7":
                hour.demand.checkout_ok.update(
                    dict.fromkeys(("web", "android"), p["checkout_ok"])
                )
                if hour.ts < state.injected_ts + timedelta(hours=1):
                    hour.demand.psp_latency["PSP-2"] = p["psp_latency_ms"]
                elif hour.ts == state.injected_ts + timedelta(hours=1):
                    add_external_status(
                        hour.batches, hour.ts, "PSP-2", "ok", "latency recovered"
                    )
            if id in ("I1", "S5"):
                category = "electronics" if id == "I1" else "home"
                for sku in sim.world.skus.values():
                    if sku.category == category:
                        sim.finance.platform_discount[sku.sku] = (
                            sku.ref_price * p["discount"]
                        )

    def _before_store(self, sim: Simulator, hour: Hour) -> None:
        for due, ts, rows in self._late[:]:
            if due <= hour.ts:
                hour.batches["orders_hourly"].extend(rows)
                with sim.store.cursor() as con:
                    con.execute(
                        "UPDATE partition_status SET loaded_rows = expected_rows WHERE table_name = 'orders_hourly' AND country = 'FR' AND ts = ?",
                        [ts],
                    )
                self._late.remove((due, ts, rows))
        for state in self.active():
            p = self.params[state.id]
            if state.id == "I2" and hour.ts < state.injected_ts + timedelta(
                hours=p["duration_hours"]
            ):
                rows = hour.batches["orders_hourly"]
                fr = [r for r in rows if r[1] == "FR"]
                loaded = round(len(fr) * p["completeness"])
                due = hour.ts + timedelta(hours=p["backfill_hours"])
                self._late.append((due, hour.ts, fr[loaded:]))
                hour.batches["orders_hourly"] = [r for r in rows if r[1] != "FR"] + fr[
                    :loaded
                ]
                hour.batches["partition_status"] = [
                    (*r[:4], loaded, due)
                    if r[0] == "orders_hourly" and r[1] == "FR"
                    else r
                    for r in hour.batches["partition_status"]
                ]
            elif state.id == "S7":
                carts = {
                    r[2]: r[3] for r in hour.batches["checkout_hourly"] if r[1] == "DE"
                }
                for r in hour.batches["checkout_hourly"]:
                    if r[1] == "FR":
                        carts[r[2]] += r[3]
                hour.batches["client_errors_hourly"] = [
                    (*r[:5], round(carts[r[1]] * p["error_share"]))
                    if r[1] in ("web", "android")
                    and r[3:5] == ("checkout", "TypeError")
                    else r
                    for r in hour.batches["client_errors_hourly"]
                ]


def inject(
    sim: Simulator, id: ScenarioId, at_ts: datetime | None = None
) -> ScenarioState:
    for hook in sim.pre_demand_hooks:
        manager = getattr(hook, "__self__", None)
        if isinstance(manager, ScenarioManager):
            return manager.inject(id, at_ts)
    return ScenarioManager(sim).inject(id, at_ts)
