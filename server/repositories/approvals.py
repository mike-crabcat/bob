"""Approval repository — human sign-off gate (Bob Events §3.4).

The approvals table predates this (migration 160, dashboard approval
workflow); Bob Events reuses it for the payment gate: the merch order
executor's precondition is ``approvals.status = 'approved'`` and nothing
auto-approves — only the ``respond_approval`` tool, driven by a human reply
in the origin conversation, flips a pending row.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from bob_server.database import Database

TERMINAL_RESPONSES = ("approved", "rejected", "cancelled")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApprovalRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        *,
        approval_type: str,
        entity_id: str,
        title: str,
        description: str = "",
        proposal: dict[str, Any] | None = None,
        requested_by: str = "",
        metadata: dict[str, Any] | None = None,
        priority: str = "normal",
    ) -> dict[str, Any]:
        aid = str(uuid.uuid4())
        await self.db.execute(
            """INSERT INTO approvals
               (id, approval_type, entity_id, title, description,
                proposal_data, status, priority, requested_at, requested_by,
                metadata)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (aid, approval_type, entity_id, title, description,
             json.dumps(proposal) if proposal else None, priority,
             _now_iso(), requested_by,
             json.dumps(metadata) if metadata else None),
        )
        row = await self.get(aid)
        return row  # type: ignore[return-value]

    async def get(self, approval_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return dict(row) if row else None

    async def pending(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM approvals WHERE status = 'pending' "
            "ORDER BY requested_at LIMIT ?", (limit,))
        return [dict(r) for r in rows] if rows else []

    async def pending_of_type(
        self, approval_type: str, *, entity_id: str | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Pending rows of one approval type, optionally one entity, oldest
        first. Call-site dedupe for request paths whose effect idempotency key
        is not deterministic (approval_request mints a uuid per call)."""
        sql = ("SELECT * FROM approvals WHERE status = 'pending' "
               "AND approval_type = ?")
        params: list[Any] = [approval_type]
        if entity_id is not None:
            sql += " AND entity_id = ?"
            params.append(entity_id)
        sql += " ORDER BY requested_at LIMIT ?"
        params.append(limit)
        rows = await self.db.fetch_all(sql, tuple(params))
        return [dict(r) for r in rows] if rows else []

    async def respond(
        self,
        approval_id: str,
        decision: str,
        *,
        reviewed_by: str = "",
        review_notes: str = "",
    ) -> dict[str, Any] | None:
        """CAS respond: only a pending row can be approved/rejected. Returns
        the updated row on success, None when already settled (the responder
        must treat that as already-handled)."""
        if decision not in TERMINAL_RESPONSES:
            raise ValueError(f"invalid approval decision {decision!r}")
        count = await self.db.execute(
            """UPDATE approvals
               SET status = ?, reviewed_at = ?, reviewed_by = ?, review_notes = ?
               WHERE id = ? AND status = 'pending'""",
            (decision, _now_iso(), reviewed_by, review_notes or None, approval_id),
        )
        if not count:
            return None
        return await self.get(approval_id)
