"""Approval tools — the human side of the payment gate (Bob Events §3.4).

An approval wake lands in the root goal's origin conversation with the cart
summary; the human's affirmative reply becomes a ``respond_approval`` tool
call. The effect executor flips the row (CAS on pending) and, for purchase
approvals, enqueues the order effect — one human action completes the flow.
The order executor independently re-checks ``status='approved'``, so nothing
spends without the recorded approval.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from bob_server.context import AppContext
from bob_server.services.tools import tool

logger = logging.getLogger(__name__)


def _summarize_proposal(proposal: Any) -> str:
    """Small human-readable summary for the approval wake. Vendor-agnostic:
    any proposal with an ``order.items`` list gets an itemised total; other
    proposals fall back to their ``summary`` field."""
    if not isinstance(proposal, dict):
        return ""
    order = proposal.get("order")
    if not isinstance(order, dict):
        return str(proposal.get("summary") or "")
    lines = []
    total = 0.0
    for item in order.get("items") or []:
        if not isinstance(item, dict):
            continue
        qty = item.get("quantity", 1) or 1
        price = item.get("retail_price")
        lines.append(f"- {qty} × {item.get('name', 'item')}"
                     + (f" (variant {item.get('variant_id')})" if item.get("variant_id") else ""))
        if isinstance(price, (int, float)):
            total += qty * price
    body = "\n".join(lines)
    return (body + (f"\nEstimated total: ${total:.2f}" if total else "")) or \
        str(proposal.get("summary") or "")


def _register_approval_executors() -> None:
    from bob_server.services import effects as effects_svc

    async def _exec_request(ctx, payload):
        from bob_server.repositories.approvals import ApprovalRepository

        row = await ApprovalRepository(ctx.db).create(
            approval_type=payload["approval_type"],
            entity_id=payload["entity_id"],
            title=payload["title"],
            description=payload.get("description", ""),
            proposal=payload.get("proposal"),
            requested_by=payload.get("requested_by", ""),
            metadata={"origin_conversation_id": payload.get("origin_conversation_id")})

        # The gate wake: the human is asked in the origin conversation with
        # the proposal summary in hand.
        origin = payload.get("origin_conversation_id")
        if origin:
            from bob_server.services.wake_service import wake_conversation
            summary = _summarize_proposal(payload.get("proposal"))
            try:
                await wake_conversation(
                    ctx, origin,
                    f"## Approval needed: {row['title']}\n"
                    f"{row['description'] or ''}\n\n{summary}\n\n"
                    "Reply to approve or reject (respond_approval).",
                    call_category="approval_request",
                    metadata={"approval_id": row["id"],
                              "approval_type": row["approval_type"]})
            except Exception:
                logger.exception("approval wake failed for %s", row["id"])
        return row["id"]

    async def _exec_respond(ctx, payload):
        from bob_server.repositories.approvals import ApprovalRepository

        repo = ApprovalRepository(ctx.db)
        row = await repo.respond(
            payload["approval_id"], payload["decision"],
            reviewed_by=payload.get("reviewed_by", ""),
            review_notes=payload.get("note", ""))
        if row is None:
            already = await repo.get(payload["approval_id"])
            if already and already["status"] == payload["decision"]:
                return already["id"]  # idempotent duplicate (effect pump retry)
            raise RuntimeError("approval already settled or not found")
        # Approving records the decision only. Acting on an approval (e.g.
        # placing an order via a skill) is the agent's job on the wake that
        # follows — by design: the platform records human intent, it doesn't
        # execute vendor actions.
        return row["id"]

    effects_svc.register_executor("approval_request", _exec_request)
    effects_svc.register_executor("approval_respond", _exec_respond)


_register_approval_executors()


def make_approval_tools(ctx: AppContext, session_key: str) -> list:
    """Approval tools for trusted conversations: list and respond."""

    @tool
    async def request_approval(
        title: str,
        description: str = "",
        approval_json: str = "{}",
    ) -> str:
        """Request human approval (e.g. for a merch order). ``approval_json``
        for purchases: {"approval_type": "purchase", "entity_id": "<goal_id>",
        "proposal": {"goal_id": ..., "summary": ..., "order": {Printful cart}}}.
        The request wakes YOUR conversation's requester with the cart
        summary; nothing is ordered until they approve via respond_approval."""
        from bob_server.services.effects import emit_and_deliver

        try:
            spec = json.loads(approval_json or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"ok": False, "error": f"approval_json invalid: {exc}"})
        if not isinstance(spec, dict):
            return json.dumps({"ok": False, "error": "approval_json must be an object"})
        approval_type = spec.get("approval_type", "purchase")
        if approval_type == "purchase" and not (spec.get("proposal") or {}).get("order"):
            return json.dumps({"ok": False,
                               "error": "purchase approvals need proposal.order (the cart)"})

        result = await emit_and_deliver(
            ctx, kind="approval_request",
            idempotency_key=f"approval_request:{uuid4()}",
            payload={"approval_type": approval_type,
                     "entity_id": spec.get("entity_id") or "unattached",
                     "title": title,
                     "description": description,
                     "proposal": spec.get("proposal"),
                     "requested_by": session_key,
                     "origin_conversation_id": spec.get(
                         "origin_conversation_id") or session_key})
        if not result.get("ok"):
            return json.dumps({"ok": False, "error": result.get("error", "failed")})
        return json.dumps({"ok": True, "approval_id": result.get("external_result_id")})

    @tool
    async def list_pending_approvals() -> str:
        """List pending approvals awaiting a human decision (id, type,
        title, description). Purchase approvals show the cart summary."""
        from bob_server.repositories.approvals import ApprovalRepository

        rows = await ApprovalRepository(ctx.db).pending()
        out = []
        for r in rows:
            item = {"approval_id": r["id"], "type": r["approval_type"],
                    "title": r["title"], "description": r["description"],
                    "requested_at": r["requested_at"]}
            if r["approval_type"] == "purchase" and r["proposal_data"]:
                try:
                    proposal = json.loads(r["proposal_data"])
                    item["summary"] = proposal.get("summary")
                except (TypeError, ValueError):
                    pass
            out.append(item)
        return json.dumps({"ok": True, "pending": out})

    @tool
    async def respond_approval(
        approval_id: str,
        decision: str,
        note: str = "",
    ) -> str:
        """Respond to a pending approval: decision is "approve" or "reject".
        Only a human reply should drive this — approving a purchase releases
        the order for placement (within the approved cart only)."""
        from bob_server.services.effects import emit_and_deliver

        decision = decision.strip().lower()
        if decision in ("approve", "yes", "y", "ok", "approved"):
            decision = "approved"
        if decision in ("reject", "no", "n", "rejected"):
            decision = "rejected"
        if decision not in ("approved", "rejected"):
            return json.dumps({"ok": False,
                               "error": 'decision must be "approve" or "reject"'})

        result = await emit_and_deliver(
            ctx, kind="approval_respond",
            idempotency_key=f"approval_respond:{approval_id}:{decision}",
            payload={"approval_id": approval_id, "decision": decision,
                     "reviewed_by": session_key, "note": note})
        if not result.get("ok"):
            return json.dumps({"ok": False, "error": result.get("error", "failed")})
        return json.dumps({"ok": True, "approval_id": approval_id,
                           "status": decision})

    return [request_approval, list_pending_approvals, respond_approval]
