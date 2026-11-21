"""Request-scoped access to SellerOS private data."""

import json
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol

from pydantic import BaseModel, Field

from selleros.models import SellerAlert, SellerAlertAck

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@contextmanager
def _journal_lock(journal: BinaryIO, *, shared: bool) -> Iterator[None]:
    """Hold the cross-process journal lock that the simulator also takes."""
    if os.name != "nt":
        fcntl.flock(journal, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
        return
    # msvcrt locks are exclusive and mandatory: locking the journal itself would
    # make concurrent reads fail instead of wait, so lock a sidecar file.
    with open(f"{journal.name}.lock", "a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


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


class SellerStore(Protocol):
    def read(self) -> Snapshot: ...

    def paused_repricers(self) -> set[tuple[str, str]]: ...

    def reverted_prices(self) -> set[tuple[str, str, datetime, float]]: ...

    def pause_repricer(self, incident_id: str, seller_id: str, sku: str) -> bool: ...

    def revert_manual_price(
        self, incident_id: str, seller_id: str, sku: str, intent: PriceIntent
    ) -> bool: ...


class FileSellerStore:
    def __init__(self, directory: Path = DATA_DIR) -> None:
        self.directory = directory
        self.lock = threading.RLock()

    def read(self) -> Snapshot:
        with self.lock:
            return Snapshot.model_validate_json(
                (self.directory / "sellers.json").read_text(encoding="utf-8")
            )

    def _records(self) -> list[dict[str, Any]]:
        try:
            with (
                (self.directory / "actions.jsonl").open("rb") as log,
                _journal_lock(log, shared=True),
            ):
                content = log.read()
        except FileNotFoundError:
            return []
        return self._complete_records(content)[0]

    @staticmethod
    def _complete_records(content: bytes) -> tuple[list[dict[str, Any]], int]:
        lines = content.splitlines(keepends=True)
        records = []
        end = 0
        for index, line in enumerate(lines):
            final = index == len(lines) - 1
            if final and not line.endswith(b"\n"):
                break
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                if final:
                    break
                raise
            records.append(record)
            end += len(line)
        return records, end

    def paused_repricers(self) -> set[tuple[str, str]]:
        with self.lock:
            return {
                (r["seller_id"], r["sku"])
                for r in self._records()
                if r["status"] == "paused"
            }

    def reverted_prices(self) -> set[tuple[str, str, datetime, float]]:
        with self.lock:
            return {
                (r["seller_id"], r["sku"], datetime.fromisoformat(r["ts"]), r["price"])
                for r in self._records()
                if r["status"] == "reverted"
            }

    def _append(self, record: dict[str, Any]) -> bool:
        with (
            (self.directory / "actions.jsonl").open("a+b") as log,
            _journal_lock(log, shared=False),
        ):
            log.seek(0)
            records, end = self._complete_records(log.read())
            if any(
                r["status"] == record["status"]
                and r["seller_id"] == record["seller_id"]
                and r["sku"] == record["sku"]
                and (
                    record["status"] == "paused"
                    or (
                        datetime.fromisoformat(r["ts"])
                        == datetime.fromisoformat(record["ts"])
                        and r["price"] == record["price"]
                    )
                )
                for r in records
            ):
                return False
            log.truncate(end)
            log.write((json.dumps(record) + "\n").encode("utf-8"))
            log.flush()
            os.fsync(log.fileno())
            return True

    def pause_repricer(self, incident_id: str, seller_id: str, sku: str) -> bool:
        with self.lock:
            return self._append(
                {
                    "incident_id": incident_id,
                    "seller_id": seller_id,
                    "sku": sku,
                    "status": "paused",
                    "action": f"paused repricer for {seller_id} on {sku}",
                }
            )

    def revert_manual_price(
        self, incident_id: str, seller_id: str, sku: str, intent: PriceIntent
    ) -> bool:
        if intent.old is None:
            raise ValueError("cannot revert a manual price without a previous price")
        with self.lock:
            return self._append(
                {
                    "incident_id": incident_id,
                    "seller_id": seller_id,
                    "sku": sku,
                    "ts": intent.ts.isoformat(),
                    "price": intent.new,
                    "restore_price": intent.old,
                    "status": "reverted",
                    "action": f"reverted manual price for {seller_id} on {sku}",
                }
            )


def receive_alert(store: SellerStore, alert: SellerAlert) -> SellerAlertAck:
    snapshot = store.read()
    revert = re.search(
        r"\brevert\b.*\bmanual\b.*\bprices?\b",
        alert.recommended_action,
        re.IGNORECASE | re.DOTALL,
    )
    actions = []
    for seller in snapshot.sellers:
        if not seller.customer_of_selleros or seller.seller_id not in alert.seller_ids:
            continue
        reverted = []
        for sku in seller.skus:
            if alert.skus and sku.sku not in alert.skus:
                continue
            if revert:
                intent = price_intent(seller, sku)
                if (
                    intent is None
                    or intent.verdict != "likely_error"
                    or (seller.seller_id, sku.sku, intent.ts, intent.new)
                    in store.reverted_prices()
                ):
                    continue
                if store.revert_manual_price(
                    alert.incident_id, seller.seller_id, sku.sku, intent
                ):
                    reverted.append(sku.sku)
            else:
                if (
                    seller.repricer_for(sku).floor is not None
                    or (seller.seller_id, sku.sku) in store.paused_repricers()
                ):
                    continue
                if store.pause_repricer(alert.incident_id, seller.seller_id, sku.sku):
                    actions.append(
                        f"paused repricer for {seller.seller_id} on {sku.sku}"
                    )
        if reverted:
            actions.append(
                f"reverted manual price for {seller.seller_id} on {', '.join(sorted(reverted))}"
            )
    return SellerAlertAck(received=True, actions_taken=actions)


class ScopedData:
    def __init__(
        self, store: SellerStore, snapshot: Snapshot, scope: Selection, as_of: datetime
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
                            if (seller.seller_id, sku.sku)
                            in self.store.paused_repricers()
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
