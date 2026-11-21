"""Deterministic change-log and external-provider event writers."""

import json
from datetime import datetime
from typing import Any

from numpy.random import Generator

from eye.sim.store import Batches

SYSTEMS = ("checkout-web", "search-ui", "catalog-svc", "app-android", "app-ios")


def add_change(
    batches: Batches,
    ts: datetime,
    system: str,
    *,
    kind: str = "release",
    planned: bool = True,
    scope: dict[str, Any] | None = None,
    description: str = "Routine release; no expected metric effect",
    expected_effect: dict[str, Any] | None = None,
) -> str:
    rows = batches.setdefault("change_log", [])
    identifier = f"change-{ts.isoformat()}-{len(rows)}"
    rows.append(
        (
            identifier,
            ts,
            system,
            kind,
            planned,
            json.dumps(scope or {}),
            description,
            json.dumps(expected_effect or {}),
        )
    )
    return identifier


def add_external_status(
    batches: Batches, ts: datetime, provider: str, status: str, description: str
) -> str:
    if status not in ("ok", "degraded", "incident"):
        raise ValueError(f"invalid external status: {status}")
    rows = batches.setdefault("external_status", [])
    identifier = f"status-{ts.isoformat()}-{len(rows)}"
    rows.append((identifier, ts, provider, status, description))
    return identifier


def noise(ts: datetime, rng: Generator, batches: Batches) -> None:
    if ts.hour == 10:
        add_change(batches, ts, str(rng.choice(SYSTEMS)))
