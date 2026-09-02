"""Effects outbox repository (Bob3 Phase I schema, exercised from Phase IV).

Every external action is recorded durably before delivery, carries a stable
idempotency key, and has observable delivery state (invariant 7). Executors
claim due effects, deliver, and mark the outcome; retrying can never
duplicate a delivered effect because claiming is a compare-and-set on
status.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from server.database import Database, Transaction

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class EffectRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def emit(
        self, *, kind: str, idempotency_key: str, payload: dict[str, Any],
        turn_id: str | None = None, txn: Transaction | None = None,
    ) -> str | None:
        """Record an effect for delivery; None if the key already exists."""
        ex: Any = txn or self.db
        effect_id = f"eff-{uuid.uuid4().hex}"
        now = _iso(_utcnow())
        inserted = await ex.execute(
            """INSERT INTO effects (id, turn_id, kind, idempotency_key, payload_json,
                                    status, attempt, available_at, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
               ON CONFLICT (idempotency_key) DO NOTHING""",
            (effect_id, turn_id, kind, idempotency_key,
             json.dumps(payload, ensure_ascii=False, default=str), now, now))
        return effect_id if inserted else None

    async def claim_due(self, *, kinds: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Atomically move due pending effects to 'delivering' and return them."""
        now = _iso(_utcnow())
        claimed: list[dict[str, Any]] = []
        async with self.db.transaction() as txn:
            if kinds:
                marks = ",".join("?" for _ in kinds)
                rows = await txn.fetch_all(
                    f"""SELECT * FROM effects
                        WHERE status = 'pending' AND available_at <= ? AND kind IN ({marks})
                        ORDER BY created_at LIMIT ?""",
                    (now, *kinds, limit))
            else:
                rows = await txn.fetch_all(
                    """SELECT * FROM effects
                       WHERE status = 'pending' AND available_at <= ?
                       ORDER BY created_at LIMIT ?""",
                    (now, limit))
            for row in rows:
                await txn.execute(
                    """UPDATE effects SET status = 'delivering', attempt = attempt + 1,
                       claimed_at = ? WHERE id = ?""",
                    (now, row["id"]))
                row["attempt"] = row["attempt"] + 1
                claimed.append(row)
        return claimed

    async def mark_delivered(self, effect_id: str, *, external_result_id: str | None = None) -> None:
        await self.db.execute(
            """UPDATE effects SET status = 'delivered', delivered_at = ?, external_result_id = ?
               WHERE id = ? AND status = 'delivering'""",
            (_iso(_utcnow()), external_result_id, effect_id))

    async def mark_failed(self, effect_id: str, error: str) -> str:
        """Fail a delivery attempt; back off and retry, or dead-letter.

        Returns the resulting status ('pending' for retry, or 'dead').
        """
        async with self.db.transaction() as txn:
            row = await txn.fetch_one("SELECT attempt FROM effects WHERE id = ?", (effect_id,))
            attempt = int(row["attempt"]) if row else 0
            if attempt >= MAX_ATTEMPTS:
                await txn.execute(
                    "UPDATE effects SET status = 'dead', error = ? WHERE id = ?",
                    (error[:2000], effect_id))
                return "dead"
            backoff = BACKOFF_BASE_SECONDS * (2 ** max(attempt - 1, 0))
            await txn.execute(
                """UPDATE effects SET status = 'pending', error = ?, available_at = ?
                   WHERE id = ?""",
                (error[:2000], _iso(_utcnow() + timedelta(seconds=backoff)), effect_id))
            return "pending"

    async def requeue_stale_delivering(
        self, *, older_than_minutes: int = 15, limit: int = 20,
    ) -> int:
        """Re-queue effects stuck in 'delivering' — a crash between the
        pending→delivering claim and the outcome write loses them otherwise
        (pump only ever claims 'pending'). Completion writes are guarded by
        ``AND status = 'delivering'`` and executors are idempotent per
        idempotency key, so a stale executor racing the redelivery can at
        worst duplicate an at-least-once delivery — the same guarantee a
        backoff retry already makes. Effects past MAX_ATTEMPTS dead-letter.

        Rows with no claimed_at (pre-reaper) fall back to created_at.
        Returns the number of effects re-queued or dead-lettered."""
        from datetime import timedelta

        cutoff = _iso(_utcnow() - timedelta(minutes=older_than_minutes))
        # julianday() parses both Python isoformat stamps (T + timezone) and
        # legacy SQLite datetime('now') stamps, so mixed vintages compare sanely.
        rows = await self.db.fetch_all(
            """SELECT id, attempt, COALESCE(claimed_at, created_at) AS claimed
               FROM effects
               WHERE status = 'delivering'
                  AND julianday(COALESCE(claimed_at, created_at)) < julianday(?)
               ORDER BY created_at LIMIT ?""",
            (cutoff, limit))
        moved = 0
        for row in rows or []:
            if int(row["attempt"]) >= MAX_ATTEMPTS:
                await self.db.execute(
                    "UPDATE effects SET status = 'dead', error = ? WHERE id = ?",
                    ("stuck in delivering; dead-lettered by reaper", row["id"]))
            else:
                await self.db.execute(
                    """UPDATE effects SET status = 'pending', available_at = ?,
                       error = COALESCE(error, '') || ' [requeued: stuck delivering]'
                       WHERE id = ?""",
                    (_iso(_utcnow()), row["id"]))
            moved += 1
        return moved

    async def status_counts(self) -> dict[str, int]:
        rows = await self.db.fetch_all(
            "SELECT status, COUNT(*) AS n FROM effects GROUP BY status")
        return {r["status"]: r["n"] for r in rows} if rows else {}

    async def dead(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT id, kind, attempt, error, payload_json, created_at
               FROM effects WHERE status = 'dead'
               ORDER BY created_at DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows] if rows else []

    async def requeue_dead(self, effect_id: str) -> bool:
        """Operator action: put a dead effect back in the queue."""
        changed = await self.db.execute(
            """UPDATE effects
               SET status = 'pending', attempt = 0,
                   available_at = strftime('%Y-%m-%dT%H:%M:%f', 'now') || '+00:00'
               WHERE id = ? AND status = 'dead'""",
            (effect_id,))
        return bool(changed)

    async def discard_dead(self, effect_id: str) -> bool:
        changed = await self.db.execute(
            """UPDATE effects
               SET status = 'discarded',
                   error = COALESCE(error, '') || ' [discarded by operator]'
               WHERE id = ? AND status = 'dead'""",
            (effect_id,))
        return bool(changed)

    async def timeline_candidates(
        self, conversation_id: str, *, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Effects for a conversation's turns plus unattributed effects; the
        caller filters unattributed rows by payload target (dashboard timeline)."""
        rows = await self.db.fetch_all(
            """SELECT id, kind, status, attempt, error, created_at,
                      COALESCE(json_extract(payload_json, '$.origin_session_key'),
                               json_extract(payload_json, '$.chat_id'),
                               json_extract(payload_json, '$.to'),
                               json_extract(payload_json, '$.session_key'), '') AS target,
                      substr(payload_json, 1, 200) AS payload_preview
               FROM effects
               WHERE turn_id IN (SELECT id FROM turns WHERE conversation_id = ?)
                  OR turn_id = '' OR turn_id IS NULL
               ORDER BY created_at DESC LIMIT ?""",
            (conversation_id, limit))
        return [dict(r) for r in rows] if rows else []

    async def get(self, effect_id: str) -> dict[str, Any] | None:
        return await self.db.fetch_one("SELECT * FROM effects WHERE id = ?", (effect_id,))

    async def get_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            "SELECT * FROM effects WHERE idempotency_key = ?", (idempotency_key,))
