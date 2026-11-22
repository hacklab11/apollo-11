"""SellerOS alert tools; never exposed to the question model."""

import asyncio
import os
import re
from collections.abc import Iterator

import httpx

from agent_runtime import resilient_client
from eye.contracts_v2 import SELLER_REVERT_TOOL, SellerPriceRevert, SellerRevertApproval
from selleros.data import SKU, PriceIntent, Seller, SellerStore, Snapshot, price_intent
from selleros.models import SellerAlert, SellerAlertAck


def selected_offers(
    snapshot: Snapshot, alert: SellerAlert
) -> Iterator[tuple[Seller, SKU]]:
    for seller in snapshot.sellers:
        if seller.customer_of_selleros and seller.seller_id in alert.seller_ids:
            for sku in seller.skus:
                if not alert.skus or sku.sku in alert.skus:
                    yield seller, sku


def proposed_revert(
    incident_id: str, intent: PriceIntent | None
) -> SellerPriceRevert | None:
    if intent is None or intent.verdict != "likely_error" or intent.old is None:
        return None
    return SellerPriceRevert(
        incident_id=incident_id,
        seller_id=intent.seller_id,
        sku=intent.sku,
        changed_at=intent.ts,
        price=intent.new,
        restore_price=intent.old,
    )


async def revert_manual_price(
    store: SellerStore,
    approvals: httpx.AsyncClient,
    action: SellerPriceRevert,
) -> bool:
    """Request approval or apply exactly the human-approved current price change."""
    key = action.idempotency_key()
    response = await approvals.put(
        "/api/approvals/seller-revert", json=action.model_dump(mode="json")
    )
    approval = SellerRevertApproval.model_validate_json(
        response.raise_for_status().content
    )
    if (
        approval.id != key
        or approval.tool_name != SELLER_REVERT_TOOL
        or approval.args != action
        or approval.status != "approved"
        or not (approval.decided_by or "").strip()
    ):
        return False
    return await asyncio.to_thread(_apply_current_revert, store, action, key)


def _apply_current_revert(
    store: SellerStore, action: SellerPriceRevert, key: str
) -> bool:
    for seller in store.read().sellers:
        if not seller.customer_of_selleros or seller.seller_id != action.seller_id:
            continue
        for sku in seller.skus:
            if sku.sku == action.sku:
                intent = price_intent(seller, sku)
                if (
                    intent is not None
                    and proposed_revert(action.incident_id, intent) == action
                ):
                    return store.revert_manual_price(
                        action.incident_id,
                        action.seller_id,
                        action.sku,
                        intent,
                        idempotency_key=key,
                    )
    return False


def _pause(store: SellerStore, alert: SellerAlert) -> list[str]:
    paused = store.paused_repricers()
    return [
        f"paused repricer for {seller.seller_id} on {sku.sku}"
        for seller, sku in selected_offers(store.read(), alert)
        if seller.repricer_for(sku).floor is None
        and (seller.seller_id, sku.sku) not in paused
        and store.pause_repricer(alert.incident_id, seller.seller_id, sku.sku)
    ]


async def receive_alert(
    store: SellerStore,
    alert: SellerAlert,
    *,
    approvals_url: str,
    approval_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SellerAlertAck:
    if not re.search(
        r"\brevert\b.*\bmanual\b.*\bprices?\b",
        alert.recommended_action,
        re.IGNORECASE | re.DOTALL,
    ):
        actions = [] if approval_id else await asyncio.to_thread(_pause, store, alert)
        return SellerAlertAck(received=True, actions_taken=actions)

    snapshot = await asyncio.to_thread(store.read)
    reverted: dict[str, list[str]] = {}
    token = os.environ.get("EYE_SELLER_REVERT_SERVICE_TOKEN")
    async with resilient_client(
        base_url=approvals_url,
        transport=transport,
        headers={"Authorization": f"Bearer {token}"} if token else None,
    ) as approvals:
        for seller, sku in selected_offers(snapshot, alert):
            action = proposed_revert(alert.incident_id, price_intent(seller, sku))
            if action is None or (
                approval_id is not None and approval_id != action.idempotency_key()
            ):
                continue
            if await revert_manual_price(store, approvals, action):
                reverted.setdefault(seller.seller_id, []).append(sku.sku)
    return SellerAlertAck(
        received=True,
        actions_taken=[
            f"reverted manual price for {seller_id} on {', '.join(sorted(skus))}"
            for seller_id, skus in reverted.items()
        ],
    )
