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

from bob_server.context import AppContext
from bob_server.services.tools import tool

logger = logging.getLogger(__name__)


def _register_approval_executors() -> None:
    from bob_server.services import effects as effects_svc

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

        if payload["decision"] == "approved" and row["approval_type"] == "purchase":
            await _enqueue_purchase_order(ctx, row)
        return row["id"]

    effects_svc.register_executor("approval_respond", _exec_respond)


async def _enqueue_purchase_order(ctx: AppContext, approval: dict[str, Any]) -> None:
    """Chain an approved purchase into the durable merch_order effect. The
    cart rides in proposal_data; the order executor re-verifies the approval
    (its precondition), so the chain is safe even if effects interleave."""
    try:
        proposal = json.loads(approval["proposal_data"] or "{}")
    except (TypeError, ValueError):
        proposal = {}
    order = proposal.get("order")
    if not order:
        logger.warning("approval %s approved but proposal has no order payload",
                       approval["id"])
        return
    from bob_server.services.effects import emit_and_deliver

    await emit_and_deliver(
        ctx, kind="merch_order",
        idempotency_key=f"merch_order:{approval['id']}",
        payload={"approval_id": approval["id"],
                 "goal_id": proposal.get("goal_id"),
                 "order": order})


_register_approval_executors()


def make_approval_tools(ctx: AppContext, session_key: str) -> list:
    """Approval tools for trusted conversations: list and respond."""

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

    return [list_pending_approvals, respond_approval]
