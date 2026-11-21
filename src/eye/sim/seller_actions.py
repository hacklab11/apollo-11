"""Seller-initiated marketplace changes and the SellerOS journal adapter."""

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import BinaryIO, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@contextmanager
def _journal_lock(journal: BinaryIO, *, shared: bool) -> Iterator[None]:
    """Hold the cross-process journal lock that SellerOS also takes."""
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


class SellerAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    seller_id: str
    sku: str
    kind: Literal["pause_repricer", "set_price"]
    price: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def price_matches_kind(self) -> Self:
        if (self.kind == "set_price") != (self.price is not None):
            raise ValueError("only set_price actions require a price")
        return self


class SellerActionFeed(Protocol):
    def pending(self) -> list[SellerAction]: ...

    def ack(self, keys: list[str]) -> None: ...


class _JournalRecord(BaseModel):
    seller_id: str
    sku: str
    status: Literal["paused", "reverted"]
    restore_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class JournalSellerActionFeed:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._acked: set[str] = set()
        self._lock = Lock()

    def ack(self, keys: list[str]) -> None:
        with self._lock:
            self._acked.update(keys)

    def pending(self) -> list[SellerAction]:
        with self._lock:
            try:
                with (
                    self.path.open("rb") as journal,
                    _journal_lock(journal, shared=True),
                ):
                    lines = journal.read().splitlines(keepends=True)
            except FileNotFoundError:
                return []
            actions = []
            for index, line in enumerate(lines):
                final = index == len(lines) - 1
                if final and not line.endswith(b"\n"):
                    break
                try:
                    payload = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if final:
                        break
                    raise
                record = _JournalRecord.model_validate(payload)
                # Historical revert records cannot restore an unknown price.
                if record.status == "reverted" and record.restore_price is None:
                    continue
                key = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if key in self._acked:
                    continue
                actions.append(
                    SellerAction(
                        key=key,
                        seller_id=record.seller_id,
                        sku=record.sku,
                        kind="set_price"
                        if record.status == "reverted"
                        else "pause_repricer",
                        price=record.restore_price
                        if record.status == "reverted"
                        else None,
                    )
                )
            return list({action.key: action for action in actions}.values())
