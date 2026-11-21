"""Drive the €9 headphones rehearsal (D6) through the public API."""

import argparse
import asyncio
import math
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from time import monotonic
from typing import Literal

import httpx
from pydantic import TypeAdapter, ValidationError

from eye.contracts import (
    AddMetricRequest,
    AddMetricResult,
    Incident,
    ScenarioState,
    SellerAlertAck,
    SentinelDecision,
)
from eye.orchestrator import Status

HOUR = timedelta(hours=1)
WAIT_HOURS = 8
NORMAL_TICKS = 3
OPEN_STATUSES = {"open", "persisting"}
SCENARIOS = TypeAdapter(list[ScenarioState])
INCIDENTS = TypeAdapter(list[Incident])
DECISIONS = TypeAdapter(list[SentinelDecision])
FEE_PER_ORDER = AddMetricRequest(
    name="fee_per_order",
    description="Seller fees in euros per item ordered.",
    unit="eur",
    sql="""
        SELECT ts, country, platform, category, seller_tier, channel,
               SUM(fee_revenue) AS num, SUM(units) AS den
        FROM orders_hourly
        GROUP BY ts, country, platform, category, seller_tier, channel
    """.strip(),
)


def say(line: str) -> None:
    print(line, flush=True)


@contextmanager
def stage(cue: str) -> Iterator[None]:
    say(cue)
    start = monotonic()
    try:
        yield
    finally:
        say(f"  Wall time: {monotonic() - start:.1f} s")


async def request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    json: dict[str, object] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    response = await client.request(method, path, json=json, params=params)
    return response.raise_for_status()


async def set_scenario(
    client: httpx.AsyncClient,
    scenario: Literal["I3", "S8"],
    action: Literal["inject", "clear"] = "inject",
) -> ScenarioState:
    response = await request(client, "POST", f"/api/scenarios/{scenario}/{action}")
    state = ScenarioState.model_validate_json(response.content)
    timestamp = state.injected_ts if action == "inject" else state.cleared_ts
    say(f"  {state.id} active={state.active} at {timestamp}")
    return state


async def step(client: httpx.AsyncClient, seconds: float) -> Status:
    await asyncio.sleep(seconds)
    response = await request(client, "POST", "/api/clock/step")
    return Status.model_validate_json(response.content)


async def prepare(client: httpx.AsyncClient, status: Status) -> None:
    response = await request(client, "GET", "/api/scenarios")
    active = {
        item.id: item
        for item in SCENARIOS.validate_json(response.content)
        if item.active
    }
    next_hour = status.sim_ts if status.tick == 0 else status.sim_ts + HOUR
    midnight = next_hour.replace(hour=0, minute=0, second=0, microsecond=0)
    if status.tick > 9 or next_hour != midnight + status.tick * HOUR:
        raise RuntimeError(
            "start with a fresh, paused world at midnight or I3 active by 09:00"
        )
    if not active and status.tick == 0:
        await set_scenario(client, "I3")
    elif set(active) != {"I3"} or active["I3"].injected_ts != midnight:
        raise RuntimeError("D6 needs only I3 active since midnight")
    for _ in range(status.tick, 9):
        await step(client, 0)
    say("  Ready at 09:00; Electronics coupon day is already active.")


async def decisions(
    client: httpx.AsyncClient, since: datetime
) -> list[SentinelDecision]:
    response = await request(
        client, "GET", "/api/anomalies", params={"since": since.isoformat()}
    )
    return DECISIONS.validate_json(response.content)


async def get_incident(client: httpx.AsyncClient, incident_id: str) -> Incident:
    while True:
        response = await request(client, "GET", f"/api/incidents/{incident_id}")
        incident = Incident.model_validate_json(response.content)
        if incident.merged_into is None:
            return incident
        incident_id = incident.merged_into


async def find_incident(
    client: httpx.AsyncClient, injected: datetime
) -> Incident | None:
    response = await request(client, "GET", "/api/incidents")
    for item in INCIDENTS.validate_json(response.content):
        if item.status not in OPEN_STATUSES or item.entry.ts < injected:
            continue
        incident = await get_incident(client, item.id)
        root = incident.root_cause
        if root and (
            root.cause_type == "price_change"
            and root.segment.get("category") == "electronics"
            and root.first_change_ts == injected
        ):
            return incident
    return None


async def incident_updates(
    client: httpx.AsyncClient, incident_id: str, seconds: float
) -> AsyncIterator[Incident]:
    for hour in range(WAIT_HOURS + 1):
        if hour:
            await step(client, seconds)
        incident = await get_incident(client, incident_id)
        incident_id = incident.id
        yield incident


def seller_reverted(incident: Incident) -> bool:
    for item in incident.notifications:
        if item.get("to") == "selleros" and item.get("ok") and item.get("ack"):
            ack = SellerAlertAck.model_validate(item["ack"])
            if ack.received and any(
                action.startswith("reverted manual price ")
                for action in ack.actions_taken
            ):
                say(f"  SellerOS: {'; '.join(ack.actions_taken)}")
                return True
    return False


async def demo(client: httpx.AsyncClient, seconds: float, record: bool) -> None:
    previous = Status.model_validate_json(
        (await request(client, "GET", "/api/status")).content
    )
    paused = Status.model_validate_json(
        (await request(client, "POST", "/api/clock/pause")).content
    )
    with stage("Prepare the 09:00 starting point (not part of the pitch):"):
        if record:
            response = await request(
                client, "POST", "/api/llm/mode", json={"mode": "record"}
            )
            say(
                f"  Core LLM mode: {Status.model_validate_json(response.content).llm_mode}"
            )
        await prepare(client, paused)

    started = monotonic()
    with stage(
        "1. Nobody asks the system anything. Show the graph: sales = units × average price, "
        "sales break down by category and seller, and price is a possible cause."
    ):
        say("  The 10% electronics coupons are planned.")

    with stage(
        "2. At 10:00 one seller enters the wrong price. Sales jump, but the hour is only "
        "partly loaded. Show why the monitor waits."
    ):
        status = await step(client, seconds)
        state = await set_scenario(client, "S8")
        injected = state.injected_ts
        if injected is None or injected != status.sim_ts + HOUR or injected.hour != 10:
            raise RuntimeError("S8 did not start at 10:00")
        await step(client, seconds)
        deferred = [
            decision
            for decision in await decisions(client, injected)
            if decision.action == "defer"
            and decision.anomaly.ts == injected
            and decision.anomaly.segment.get("category") == "electronics"
        ]
        if deferred:
            say(f"  Electronics decision: {deferred[0].reason}")
        else:
            say("  No electronics defer decision was observed in the first S8 hour.")

    with stage(
        "3. Once the hour fills, compare the coupon explanation with the seller's price drop. "
        "Use measured shares; stockouts and Buy Box moves are downstream. Intent needs SellerOS evidence."
    ):
        for _ in range(WAIT_HOURS):
            status = await step(client, seconds)
            incident = await find_incident(client, injected)
            if incident is not None:
                say(f"  {incident.id}: {incident.verdict} — {incident.summary}")
                break
        else:
            raise RuntimeError("no open electronics price-change incident for S8")

    with stage(
        "4. Add fee_per_order with SQL only. Show fees = units × fee per order, "
        "the seller breakdown, and the possible link from average price. "
        "Watch small third-party electronics sellers for new evidence on the same incident."
    ):
        response = await request(
            client,
            "POST",
            "/api/metrics",
            json=FEE_PER_ORDER.model_dump(exclude_none=True),
        )
        result = AddMetricResult.model_validate_json(response.content)
        say(
            f"  Added {result.metric.name}: {len(result.accepted)} accepted, "
            f"{len(result.ambiguous)} ambiguous, {len(result.rejected)} rejected edges."
        )
        since = status.sim_ts + HOUR
        async for current in incident_updates(client, incident.id, seconds):
            if current.status not in OPEN_STATUSES:
                raise RuntimeError(
                    "the S8 incident closed before fee_per_order attached"
                )
            if any(
                decision.action == "attach"
                and decision.attach_to_incident == current.id
                and decision.anomaly.metric == FEE_PER_ORDER.name
                and decision.anomaly.segment.get("category") == "electronics"
                and decision.anomaly.segment.get("seller_tier") == "small_3P"
                for decision in await decisions(client, since)
            ):
                say(
                    f"  {result.metric.name} attached to {current.id}; occurrences={current.occurrences}."
                )
                incident = current
                break
        else:
            raise RuntimeError(
                "electronics / small_3P fee_per_order did not attach to the S8 incident"
            )

    with stage(
        "5. Show SellerOS confirming the error and reverting the price; follow the same incident to resolution."
    ):
        async for current in incident_updates(client, incident.id, seconds):
            if current.status not in OPEN_STATUSES:
                raise RuntimeError(
                    "the S8 incident closed before SellerOS acknowledged the revert"
                )
            if current.verdict == "CONFIRMED" and seller_reverted(current):
                incident = current
                break
        else:
            raise RuntimeError(
                "the S8 incident did not show confirmation and a SellerOS price-revert acknowledgement"
            )
        cleared = (await set_scenario(client, "S8", "clear")).cleared_ts
        if cleared is None:
            raise RuntimeError("S8 did not clear")
        async for current in incident_updates(client, incident.id, seconds):
            say(
                f"  {current.id}: {current.status}; normal ticks={current.normal_ticks}; occurrences={current.occurrences}."
            )
            if current.status == "resolved":
                if (
                    current.resolved_ts is None
                    or current.resolved_ts < cleared + (NORMAL_TICKS - 1) * HOUR
                    or current.normal_ticks < NORMAL_TICKS
                ):
                    raise RuntimeError(
                        "the S8 incident resolved before three normal recovery ticks"
                    )
                break
        else:
            raise RuntimeError(
                "the S8 incident did not resolve after reverting the price"
            )

    say(
        f"D6 rehearsal: {monotonic() - started:.1f} s wall time (target: 180 s, excludes setup)."
    )
    if not previous.paused:
        await request(client, "POST", "/api/clock/resume")


def seconds_per_hour(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        seconds = -1
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("expected finite, nonnegative seconds")
    return seconds


async def run(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient(base_url=args.base_url, timeout=300.0) as client:
        await demo(client, args.speed, args.record)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", choices=["demo"], required=True)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--speed",
        type=seconds_per_hour,
        default=5.0,
        help="seconds per simulated hour (default: 5)",
    )
    parser.add_argument(
        "--record", action="store_true", help="record core LLM responses for replay"
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except httpx.HTTPStatusError as exc:
        print(
            f"Demo stopped: {exc.request.method} {exc.request.url.path} "
            f"returned HTTP {exc.response.status_code}.",
            file=sys.stderr,
        )
        return 1
    except (httpx.RequestError, httpx.InvalidURL, ValidationError) as exc:
        print(
            f"Demo stopped: {type(exc).__name__}. Check the base URL and core API.",
            file=sys.stderr,
        )
        return 1
    except RuntimeError as exc:
        print(f"Demo stopped: {exc}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
