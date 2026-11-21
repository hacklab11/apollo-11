"""Request-scoped access to SellerOS private data."""

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from selleros.models import SellerAlert, SellerAlertAck

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


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
    repricer: dict[str, Any] | None = None


class Price(BaseModel):
    ts: datetime
    sku: str
    price: float


class Seller(BaseModel):
    seller_id: str
    customer_of_selleros: bool
    repricer: Repricer
    skus: list[SKU]
    price_history: list[Price]


class Snapshot(BaseModel):
    generated_for_seed: int
    sellers: list[Seller]


class Selection(BaseModel):
    seller_ids: list[str] = Field(default_factory=list)
    skus: list[str] = Field(default_factory=list)


class ToolOutput(BaseModel):
    rows: list[dict[str, Any]]
    summary: str = Field(max_length=300)


class Store:
    def __init__(self, directory: Path = DATA_DIR) -> None:
        self.directory = directory
        self.paused: set[str] = set()
        self.lock = threading.Lock()

    def read(self) -> Snapshot:
        return Snapshot.model_validate_json(
            (self.directory / "sellers.json").read_text(encoding="utf-8")
        )

    def pause(self, snapshot: Snapshot, alert: SellerAlert) -> SellerAlertAck:
        actions = []
        with self.lock:
            for seller in snapshot.sellers:
                if (
                    seller.customer_of_selleros
                    and seller.seller_id in alert.seller_ids
                    and seller.repricer.floor is None
                    and seller.seller_id not in self.paused
                    and (not alert.skus or any(s.sku in alert.skus for s in seller.skus))
                ):
                    action = f"paused repricer for {seller.seller_id}"
                    with (self.directory / "actions.jsonl").open("a", encoding="utf-8") as log:
                        log.write(json.dumps({"incident_id": alert.incident_id, "seller_id": seller.seller_id, "status": "paused", "action": action}) + "\n")
                    self.paused.add(seller.seller_id)
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
            and (seller.seller_id in scope.seller_ids or any(s.sku in scope.skus for s in seller.skus))
        ]
        for seller in self.sellers:
            if seller.seller_id not in scope.seller_ids:
                seller.skus = [s for s in seller.skus if s.sku in scope.skus]
                seller.price_history = [p for p in seller.price_history if p.sku in scope.skus]

    def query(self, name: str, selection: Selection) -> ToolOutput:
        rows: list[dict[str, Any]] = []
        for seller in self.sellers:
            if selection.seller_ids and seller.seller_id not in selection.seller_ids:
                continue
            skus = [s for s in seller.skus if not selection.skus or s.sku in selection.skus]
            if not skus:
                continue
            sku_ids = {s.sku for s in skus}
            history = sorted(
                [p for p in seller.price_history if p.sku in sku_ids and p.ts <= self.as_of],
                key=lambda p: p.ts,
            )
            base = {"seller_id": seller.seller_id}
            if name == "get_repricer":
                rows.append(base | seller.repricer.model_dump() | {
                    "status": "paused" if seller.seller_id in self.store.paused else seller.repricer.status,
                })
            elif name == "get_price_history":
                recent = [p.model_dump(mode="json") for p in history if p.ts >= self.as_of - timedelta(hours=24)]
                rows.append(base | {"price_path": recent, "price_changes": len(recent)})
            elif name in {"get_costs", "get_supplier_eta"}:
                for sku in skus:
                    prices = [p for p in history if p.sku == sku.sku]
                    if name == "get_costs":
                        price = prices[-1].price if prices else None
                        rows.append(base | {"sku": sku.sku, "unit_cost": sku.unit_cost, "price": price, "margin_per_unit": round(price - sku.unit_cost, 6) if price is not None else None})
                    else:
                        rows.append(base | {"sku": sku.sku, "supplier_eta_days": sku.supplier_eta_days, "expected_restock_ts": (self.as_of + timedelta(days=sku.supplier_eta_days)).isoformat()})
            else:
                raise ValueError("unknown data tool")
        return ToolOutput(rows=rows, summary=f"{len(rows)} scoped rows from {name}")
