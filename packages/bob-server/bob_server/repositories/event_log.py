"""Append-only event log repository (Bob3 Phase I).

Events are the durable record of accepted stimuli. Rows are never updated
or deleted. Uniqueness on ``(source, external_id)`` enforces invariant 1
(accept-once); appends composed with source persistence in one transaction
enforce invariant 2.

Until Phase VI introduces real conversations/bindings, both
``binding_key`` and ``conversation_id`` carry the session_key — the plan's
ground rule ("session_key survives as the binding key") makes the Phase VI
backfill mechanical.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from bob_server.database import Database, Transaction


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_event_id() -> str:
    """Time-ordered event id: lexicographic order == append order.

    Nanosecond timestamp prefix (zero-padded) keeps ids sortable, which
    turn watermarks (invariant 5) rely on; the random suffix breaks ties.
    """
    return f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"


@dataclass(slots=True)
class Event:
    event_type: str
    binding_key: str
    conversation_id: str
    source: str
    external_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    causation_id: str | None = None
    correlation_id: str | None = None
    occurred_at: str | None = None      # defaults to recorded_at
    schema_version: int = 1
    id: str = field(default_factory=new_event_id)


class EventLogRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def append(self, event: Event, *, txn: Transaction | None = None) -> str | None:
        """Append an event; return its id, or None if it was a duplicate.

        Duplicate = an event with the same ``(source, external_id)`` already
        exists (invariant 1). Pass ``txn`` to make the append atomic with
        the source-table write (invariant 2).
        """
        ex: Any = txn or self.db
        recorded_at = _utcnow_iso()
        inserted = await ex.execute(
            """INSERT INTO event_log
                   (id, event_type, schema_version, binding_key, conversation_id,
                    source, external_id, causation_id, correlation_id,
                    occurred_at, recorded_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, external_id) WHERE external_id IS NOT NULL
               DO NOTHING""",
            (
                event.id, event.event_type, event.schema_version,
                event.binding_key, event.conversation_id,
                event.source, event.external_id,
                event.causation_id, event.correlation_id,
                event.occurred_at or recorded_at, recorded_at,
                json.dumps(event.payload, ensure_ascii=False, default=str),
            ),
        )
        return event.id if inserted else None

    async def get(self, event_id: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            "SELECT * FROM event_log WHERE id = ?", (event_id,))

    async def events_after(
        self, event_type: str, after_id: str, *, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Events of one type strictly after an id, in id order — the claim
        router's watermark replay reads through here (Bob Events §2.2)."""
        rows = await self.db.fetch_all(
            "SELECT * FROM event_log WHERE event_type = ? AND id > ? "
            "ORDER BY id LIMIT ?",
            (event_type, after_id, limit))
        return [dict(r) for r in rows] if rows else []

    async def find_by_external_id(self, source: str, external_id: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            "SELECT * FROM event_log WHERE source = ? AND external_id = ?",
            (source, external_id))

    async def pending_for_conversation(
        self, conversation_id: str, *, after_id: str = "", limit: int = 200,
        txn: Transaction | None = None,
    ) -> list[dict[str, Any]]:
        """Events for a conversation not yet claimed by any turn, id-ordered."""
        ex: Any = txn or self.db
        return await ex.fetch_all(
            """SELECT e.* FROM event_log e
               WHERE e.conversation_id = ? AND e.id > ?
                 AND NOT EXISTS (SELECT 1 FROM turn_events te WHERE te.event_id = e.id)
               ORDER BY e.id LIMIT ?""",
            (conversation_id, after_id, limit))

    async def first_recorded_at(self, source: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT MIN(recorded_at) AS t FROM event_log WHERE source = ?", (source,))
        return row["t"] if row else None

    async def redact_contact_payloads(self, contact_id: str) -> int:
        """Deletion propagation: blank payloads that referenced a deleted contact."""
        return await self.db.execute(
            """UPDATE event_log
               SET payload_json = json_object('redacted', 'contact_deleted')
               WHERE json_extract(payload_json, '$.contact_id') = ?
                 AND json_extract(payload_json, '$.redacted') IS NULL""",
            (contact_id,))

    async def count_since(self, source: str, since_recorded_at: str) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM event_log WHERE source = ? AND recorded_at >= ?",
            (source, since_recorded_at))
        return int(row["n"]) if row else 0
