"""Turn repository — durable leases, watermarks, attempts (Bob3 Phase I).

Enforces invariants 4-6: at most one active turn per conversation (partial
unique index + BEGIN IMMEDIATE claim), a turn's input set is fixed before
execution (turn_events written at claim time), and failure never silently
consumes inputs (release/retry/dead-letter transitions are explicit).

Not wired to a live dispatcher until Phase III; Phase I ships the protocol
so it hardens under test first.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from bob_server.database import Database

MAX_ATTEMPTS = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TurnRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def claim(
        self, conversation_id: str, *, lease_owner: str,
        lease_seconds: int = 300, event_limit: int = 200,
    ) -> dict[str, Any] | None:
        """Atomically create a running turn claiming all pending events.

        Returns ``{"turn_id", "event_ids", "watermark"}`` or None when there
        is nothing to claim or another turn is active for the conversation.
        The whole claim runs under BEGIN IMMEDIATE so two owners can never
        claim the same events (invariants 4, 5, 10).
        """
        now = _utcnow()
        async with self.db.transaction() as txn:
            active = await txn.fetch_one(
                """SELECT id, lease_expires_at FROM turns
                   WHERE conversation_id = ? AND status IN ('pending', 'running')""",
                (conversation_id,))
            if active:
                if active["lease_expires_at"] and active["lease_expires_at"] > _iso(now):
                    return None
                # Expired lease: fail the zombie turn and release its claims.
                await self._release_expired(txn, active["id"], now)

            events = await txn.fetch_all(
                """SELECT e.id FROM event_log e
                   WHERE e.conversation_id = ?
                     AND NOT EXISTS (SELECT 1 FROM turn_events te WHERE te.event_id = e.id)
                   ORDER BY e.id LIMIT ?""",
                (conversation_id, event_limit))
            if not events:
                return None

            event_ids = [r["id"] for r in events]
            watermark = event_ids[-1]
            # Carry attempts across re-claims: any failed turn whose watermark
            # covers our first event had claimed it before (claims always take
            # the oldest pending events), so this is a retry of that input.
            prior = await txn.fetch_one(
                """SELECT COUNT(*) AS n FROM turns
                   WHERE conversation_id = ? AND status = 'failed'
                     AND input_high_watermark >= ?""",
                (conversation_id, event_ids[0]))
            attempt = 1 + (int(prior["n"]) if prior else 0)
            turn_id = f"turn-{uuid.uuid4().hex}"
            await txn.execute(
                """INSERT INTO turns (id, conversation_id, status, input_high_watermark,
                                      lease_owner, lease_expires_at, attempt, started_at, created_at)
                   VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)""",
                (turn_id, conversation_id, watermark, lease_owner,
                 _iso(now + timedelta(seconds=lease_seconds)), attempt, _iso(now), _iso(now)))
            for eid in event_ids:
                await txn.execute(
                    "INSERT INTO turn_events (turn_id, event_id) VALUES (?, ?)",
                    (turn_id, eid))
            return {"turn_id": turn_id, "event_ids": event_ids, "watermark": watermark}

    async def _release_expired(self, txn: Any, turn_id: str, now: datetime) -> None:
        """Fail an expired turn and release its event claims for re-claim."""
        row = await txn.fetch_one("SELECT attempt FROM turns WHERE id = ?", (turn_id,))
        attempt = int(row["attempt"]) if row else 0
        status = "dead" if attempt >= MAX_ATTEMPTS else "failed"
        await txn.execute(
            "UPDATE turns SET status = ?, error = COALESCE(error, 'lease expired'), completed_at = ? WHERE id = ?",
            (status, _iso(now), turn_id))
        if status != "dead":
            await txn.execute("DELETE FROM turn_events WHERE turn_id = ?", (turn_id,))

    async def complete(self, turn_id: str) -> None:
        await self.db.execute(
            "UPDATE turns SET status = 'succeeded', completed_at = ? WHERE id = ? AND status = 'running'",
            (_iso(_utcnow()), turn_id))

    async def fail(self, turn_id: str, error: str, *, release_events: bool = True) -> str:
        """Mark a turn failed; release claims for retry unless dead (invariant 6).

        Returns the resulting status ('failed' or 'dead').
        """
        now = _utcnow()
        async with self.db.transaction() as txn:
            row = await txn.fetch_one("SELECT attempt FROM turns WHERE id = ?", (turn_id,))
            attempt = int(row["attempt"]) if row else 0
            status = "dead" if attempt >= MAX_ATTEMPTS else "failed"
            await txn.execute(
                "UPDATE turns SET status = ?, error = ?, completed_at = ? WHERE id = ?",
                (status, error[:2000], _iso(now), turn_id))
            if status != "dead" and release_events:
                await txn.execute("DELETE FROM turn_events WHERE turn_id = ?", (turn_id,))
            return status

    async def heartbeat_lease(self, turn_id: str, *, lease_seconds: int = 300) -> bool:
        n = await self.db.execute(
            "UPDATE turns SET lease_expires_at = ? WHERE id = ? AND status = 'running'",
            (_iso(_utcnow() + timedelta(seconds=lease_seconds)), turn_id))
        return n > 0

    async def stuck(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Turns still 'running' with an expired lease — a crash or hang."""
        rows = await self.db.fetch_all(
            """SELECT id, conversation_id, attempt, started_at, lease_expires_at
               FROM turns
               WHERE status = 'running'
                 AND lease_expires_at IS NOT NULL
                 AND lease_expires_at < strftime('%Y-%m-%dT%H:%M:%f', 'now')
               ORDER BY started_at LIMIT ?""", (limit,))
        return [dict(r) for r in rows] if rows else []

    async def stuck_check(self, turn_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            """SELECT id, conversation_id, status,
                  (status IN ('pending','running')
                   AND lease_expires_at IS NOT NULL
                   AND lease_expires_at < strftime('%Y-%m-%dT%H:%M:%f', 'now')) AS stuck
               FROM turns WHERE id = ?""", (turn_id,))
        return dict(row) if row else None

    async def get(self, turn_id: str) -> dict[str, Any] | None:
        return await self.db.fetch_one("SELECT * FROM turns WHERE id = ?", (turn_id,))

    async def events_for(self, turn_id: str) -> list[str]:
        rows = await self.db.fetch_all(
            "SELECT event_id FROM turn_events WHERE turn_id = ? ORDER BY event_id",
            (turn_id,))
        return [r["event_id"] for r in rows]
