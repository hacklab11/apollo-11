"""Request-scoped access to SellerOS private data."""

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from selleros.models import SellerAlert, SellerAlertAck

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
PriceSource = Literal["repricer", "manual"]


class Repricer(BaseModel):
    mode: str
    undercut_pct: float = 1.0
    floor: float | None
    status: str = "active"


class SKU(BaseModel):
    sku: str
    unit_cost: float
    stock_policy_days: int
    supplier_eta_days: int
    category: str | None = None
    repricer: Repricer | None = None


class Price(BaseModel):
    ts: datetime
    sku: str
    price: float
    source: PriceSource = "repricer"


class Seller(BaseModel):
    seller_id: str
    customer_of_selleros: bool
    repricer: Repricer
    skus: list[SKU]
    price_history: list[Price]

    def repricer_for(self, sku: SKU) -> Repricer:
        if sku.repricer is None:
            return self.repricer
        return self.repricer.model_copy(
            update=sku.repricer.model_dump(exclude_unset=True)
        )


class Snapshot(BaseModel):
    generated_for_seed: int
    sellers: list[Seller]


class Selection(BaseModel):
    seller_ids: list[str] = Field(default_factory=list)
    skus: list[str] = Field(default_factory=list)


class ToolOutput(BaseModel):
    rows: list[dict[str, Any]]
    summary: str = Field(max_length=300)


class PriceIntent(BaseModel):
    seller_id: str
    sku: str
    ts: datetime
    old: float | None
    new: float
    source: PriceSource
    unit_cost: float
    verdict: Literal["intended", "likely_error"]


def price_intent(
    seller: Seller, sku: SKU, as_of: datetime | None = None
) -> PriceIntent | None:
    history = sorted(
        (
            p
            for p in seller.price_history
            if p.sku == sku.sku and (as_of is None or p.ts <= as_of)
        ),
        key=lambda p: p.ts,
    )
    if not history:
        return None
    latest = history[-1]
    return PriceIntent(
        seller_id=seller.seller_id,
        sku=sku.sku,
        ts=latest.ts,
        old=history[-2].price if len(history) > 1 else None,
        new=latest.price,
        source=latest.source,
        unit_cost=sku.unit_cost,
        verdict="likely_error"
        if latest.source == "manual" and latest.price < sku.unit_cost
        else "intended",
    )


class Store:
    def __init__(self, directory: Path = DATA_DIR) -> None:
        self.directory = directory
        self.paused: set[tuple[str, str]] = set()
        self.reverted: set[tuple[str, str, datetime, float]] = set()
        self.lock = threading.Lock()

    def read(self) -> Snapshot:
        return Snapshot.model_validate_json(
            (self.directory / "sellers.json").read_text(encoding="utf-8")
        )

    def receive_alert(self, snapshot: Snapshot, alert: SellerAlert) -> SellerAlertAck:
        if re.search(
            r"\brevert\b.*\bmanual\b.*\bprices?\b",
            alert.recommended_action,
            re.IGNORECASE | re.DOTALL,
        ):
            return self.revert_manual_price(snapshot, alert)
        return self.pause(snapshot, alert)

    def revert_manual_price(
        self, snapshot: Snapshot, alert: SellerAlert
    ) -> SellerAlertAck:
        actions = []
        with self.lock:
            for seller in snapshot.sellers:
                if (
                    not seller.customer_of_selleros
                    or seller.seller_id not in alert.seller_ids
                ):
                    continue
                intents = [
                    intent
                    for sku in seller.skus
                    if (not alert.skus or sku.sku in alert.skus)
                    and (intent := price_intent(seller, sku)) is not None
                    and intent.verdict == "likely_error"
                    and (intent.seller_id, intent.sku, intent.ts, intent.new)
                    not in self.reverted
                ]
                if not intents:
                    continue
                skus = sorted(intent.sku for intent in intents)
                action = (
                    f"reverted manual price for {seller.seller_id} on {', '.join(skus)}"
                )
                with (self.directory / "actions.jsonl").open(
                    "a", encoding="utf-8"
                ) as log:
                    log.write(
                        json.dumps(
                            {
                                "incident_id": alert.incident_id,
                                "seller_id": seller.seller_id,
                                "skus": skus,
                                "status": "reverted",
                                "action": action,
                            }
                        )
                        + "\n"
                    )
                self.reverted.update(
                    (intent.seller_id, intent.sku, intent.ts, intent.new)
                    for intent in intents
                )
                actions.append(action)
        return SellerAlertAck(received=True, actions_taken=actions)

    def pause(self, snapshot: Snapshot, alert: SellerAlert) -> SellerAlertAck:
        actions = []
        with self.lock:
            for seller in snapshot.sellers:
                if (
                    not seller.customer_of_selleros
                    or seller.seller_id not in alert.seller_ids
                ):
                    continue
                for sku in seller.skus:
                    key = (seller.seller_id, sku.sku)
                    if (
                        (alert.skus and sku.sku not in alert.skus)
                        or seller.repricer_for(sku).floor is not None
                        or key in self.paused
                    ):
                        continue
                    action = f"paused repricer for {seller.seller_id} on {sku.sku}"
                    with (self.directory / "actions.jsonl").open(
                        "a", encoding="utf-8"
                    ) as log:
                        log.write(
                            json.dumps(
                                {
                                    "incident_id": alert.incident_id,
                                    "seller_id": seller.seller_id,
                                    "sku": sku.sku,
                                    "status": "paused",
                                    "action": action,
                                }
                            )
                            + "\n"
                        )
                    self.paused.add(key)
                    actions.append(action)
        return SellerAlertAck(received=True, actions_taken=actions)


class ScopedData:
    def __init__(
        self, store: Store, snapshot: Snapshot, scope: Selection, as_of: datetime
    ) -> None:
        self.store = store
        self.as_of = as_of
        self.sellers = [
            seller.model_copy(deep=True)
            for seller in snapshot.sellers
            if seller.customer_of_selleros
            and (
                seller.seller_id in scope.seller_ids
                or any(s.sku in scope.skus for s in seller.skus)
            )
        ]
        for seller in self.sellers:
            if seller.seller_id not in scope.seller_ids:
                seller.skus = [s for s in seller.skus if s.sku in scope.skus]
                seller.price_history = [
                    p for p in seller.price_history if p.sku in scope.skus
                ]

    def query(self, name: str, selection: Selection) -> ToolOutput:
        rows: list[dict[str, Any]] = []
        for seller in self.sellers:
            if selection.seller_ids and seller.seller_id not in selection.seller_ids:
                continue
            skus = [
                s for s in seller.skus if not selection.skus or s.sku in selection.skus
            ]
            if not skus:
                continue
            sku_ids = {s.sku for s in skus}
            history = sorted(
                [
                    p
                    for p in seller.price_history
                    if p.sku in sku_ids and p.ts <= self.as_of
                ],
                key=lambda p: p.ts,
            )
            base = {"seller_id": seller.seller_id}
            if name == "get_repricer":
                for sku in skus:
                    repricer = seller.repricer_for(sku)
                    rows.append(
                        base
                        | {"sku": sku.sku}
                        | repricer.model_dump()
                        | {
                            "status": "paused"
                            if (seller.seller_id, sku.sku) in self.store.paused
                            else repricer.status,
                        }
                    )
            elif name == "get_price_history":
                recent = [
                    p.model_dump(mode="json")
                    for p in history
                    if p.ts >= self.as_of - timedelta(hours=24)
                ]
                rows.append(base | {"price_path": recent, "price_changes": len(recent)})
            elif name == "price_intent_check":
                for sku in skus:
                    intent = price_intent(seller, sku, self.as_of)
                    if intent is not None:
                        rows.append(intent.model_dump(mode="json"))
            elif name in {"get_costs", "get_supplier_eta"}:
                for sku in skus:
                    prices = [p for p in history if p.sku == sku.sku]
                    if name == "get_costs":
                        price = prices[-1].price if prices else None
                        rows.append(
                            base
                            | {
                                "sku": sku.sku,
                                "unit_cost": sku.unit_cost,
                                "price": price,
                                "margin_per_unit": round(price - sku.unit_cost, 6)
                                if price is not None
                                else None,
                            }
                        )
                    else:
                        rows.append(
                            base
                            | {
                                "sku": sku.sku,
                                "supplier_eta_days": sku.supplier_eta_days,
                                "expected_restock_ts": (
                                    self.as_of + timedelta(days=sku.supplier_eta_days)
                                ).isoformat(),
                            }
                        )
            else:
                raise ValueError("unknown data tool")
        return ToolOutput(rows=rows, summary=f"{len(rows)} scoped rows from {name}")
