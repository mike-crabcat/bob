"""Merch service — print-on-demand ordering behind the payment gate.

Bob Events §3.4: the merch_order effect executor. Hard preconditions, in
order:
1. ``settings.merch.enabled`` — off by default (no account configured ⇒ no
   possible spend).
2. The linked approvals row is ``approved`` — the recorded human decision.
   Nothing in this module creates or flips approvals.
3. The API key exists in the configured file under config_dir — file-based
   credentials, deliberately NOT environment variables (the agent's bash
   must never be able to read them; same class as the Twilio-creds finding).

Orders are sent with Printful's ``external_id`` set to the approval id, so a
crash between "order placed" and "effect recorded" cannot double-order on
pump retry. On success the linked goal settles through the chokepoint, so
the result rolls up into the plan's state like any other child outcome.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from bob_server.context import AppContext

logger = logging.getLogger(__name__)


def _api_key(ctx: AppContext) -> str:
    settings = ctx.settings
    key_file = settings.config_dir / settings.merch.api_key_file
    try:
        key = key_file.read_text().strip()
    except OSError as exc:
        raise RuntimeError(
            f"merch API key unavailable at {key_file} "
            "(drop the key there with mode 0600; never use env vars)") from exc
    if not key:
        raise RuntimeError(f"merch API key file {key_file} is empty")
    return key


async def place_order(
    ctx: AppContext, *, approval_id: str, order: dict[str, Any],
) -> str:
    """Place one print-on-demand order. Returns the vendor's order id."""
    settings = ctx.settings

    # The approval gate is the safety property — check it BEFORE the
    # enabled/config checks so a misconfiguration can never mask it.
    from bob_server.repositories.approvals import ApprovalRepository

    approval = await ApprovalRepository(ctx.db).get(approval_id)
    if approval is None or approval["status"] != "approved":
        raise RuntimeError(
            f"refusing to order: approval {approval_id} is not approved "
            "(nothing spends without the recorded human decision)")
    if not settings.merch.enabled:
        raise RuntimeError("merch ordering is disabled (BOB_MERCH_ENABLED)")

    order = dict(order)
    order.setdefault("external_id", f"bob-{approval_id}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.merch.api_base.rstrip('/')}/v2/orders",
            headers={"Authorization": f"Bearer {_api_key(ctx)}"},
            json=order,
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"merch order failed: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    result = data.get("result") or data
    order_id = str(result.get("id") or result.get("code") or approval_id)
    logger.info("merch order %s placed for approval %s", order_id, approval_id)
    return order_id


def _register_merch_executors() -> None:
    from bob_server.services import effects as effects_svc

    async def _exec_merch_order(ctx, payload):
        order_id = await place_order(
            ctx, approval_id=payload["approval_id"], order=payload["order"])
        goal_id = payload.get("goal_id")
        if goal_id:
            from bob_server.services.goal_service import settle_goal
            ok = await settle_goal(
                ctx, goal_id, status="completed",
                result=f"POD order {order_id} placed (approval "
                       f"{payload['approval_id'][:8]}…)")
            if not ok:
                logger.warning("merch order %s: linked goal %s already settled",
                               order_id, goal_id)
        return order_id

    # Retryable: the approval precondition re-checks on every attempt and
    # external_id dedupes on the vendor side, so a crash-retry cannot
    # double-order.
    effects_svc.register_executor("merch_order", _exec_merch_order,
                                  retryable=True)


_register_merch_executors()


def build_cart_summary(order: dict[str, Any]) -> str:
    """Human-readable cart summary for the approval wake (§3.4)."""
    items = order.get("items") or []
    lines = [f"- {i.get('quantity', 1)} × {i.get('name', 'item')} "
             f"(variant {i.get('variant_id', '?')})" for i in items]
    total = sum(
        (i.get("quantity", 1) or 1) * (i.get("retail_price") or 0)
        for i in items if isinstance(i.get("retail_price"), (int, float)))
    body = "\n".join(lines) if lines else "(no items)"
    suffix = f"\nEstimated total: ${total:.2f}" if total else ""
    return body + suffix
