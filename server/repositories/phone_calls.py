"""Phone call repository — owns all phone_calls SQL.

One row per call: Twilio phone calls (inbound/outbound) and browser
voice-link sessions (direction='voice_link', mirrored from voice_sessions
with the same id). Result dispatch is claimed atomically via
result_dispatched_at so a call's outcome is relayed exactly once across
restarts and concurrent webhook deliveries.
"""

from __future__ import annotations

from typing import Any


class PhoneCallRepository:
    def __init__(self, db: Any):
        self.db = db

    # ------------------------------------------------------------ inserts

    async def insert_inbound(
        self, *, call_id: str, call_sid: str, phone_number: str, agenda: str | None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO phone_calls (id, call_sid, phone_number, direction, status, agenda, started_at)
               VALUES (?, ?, ?, 'inbound', 'ringing', ?, datetime('now'))""",
            (call_id, call_sid, phone_number, agenda))

    async def insert_outbound(
        self,
        *,
        call_id: str,
        call_sid: str,
        phone_number: str,
        agenda: str | None,
        engine: str,
        realtime_meta_json: str,
        subagent_id: str | None,
        origin_session_key: str | None,
    ) -> None:
        await self.db.execute(
            """INSERT INTO phone_calls
               (id, call_sid, phone_number, direction, status, agenda, engine,
                realtime_meta, subagent_id, origin_session_key, started_at)
               VALUES (?, ?, ?, 'outbound', 'ringing', ?, ?, ?, ?, ?, datetime('now'))""",
            (call_id, call_sid, phone_number, agenda, engine,
             realtime_meta_json, subagent_id, origin_session_key))

    async def insert_voice_link(
        self,
        *,
        token: str,
        phone_number: str,
        goal: str | None,
        subagent_id: str | None,
        origin_session_key: str | None,
    ) -> None:
        """Mirror row for a browser voice-link session (same id as the
        voice_sessions row) so links appear in the calls UI."""
        await self.db.execute(
            """INSERT INTO phone_calls
               (id, call_sid, phone_number, direction, status, agenda, engine,
                subagent_id, origin_session_key, started_at)
               VALUES (?, '', ?, 'voice_link', 'ringing', ?, 'openai_realtime', ?, ?, datetime('now'))""",
            (token, phone_number, goal, subagent_id, origin_session_key))

    # -------------------------------------------------------------- reads

    async def get(self, id_or_sid: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM phone_calls WHERE id = ? OR call_sid = ?",
            (id_or_sid, id_or_sid))
        return dict(row) if row else None

    async def get_by_sid(self, call_sid: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM phone_calls WHERE call_sid = ?", (call_sid,))
        return dict(row) if row else None

    async def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT id, call_sid, phone_number, direction, status, agenda,
                      exchange_count, duration_seconds, recording_path,
                      started_at, completed_at
               FROM phone_calls
               ORDER BY started_at DESC
               LIMIT ?""", (limit,))
        return [dict(r) for r in rows] if rows else []

    async def recent_with_contacts(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT pc.id, pc.call_sid, pc.phone_number, pc.direction, pc.status,
                      pc.agenda, pc.exchange_count, pc.duration_seconds, pc.recording_path,
                      pc.started_at, pc.completed_at,
                      c.id as contact_id, c.name as contact_name
               FROM phone_calls pc
               LEFT JOIN contacts c ON c.phone_number = pc.phone_number AND c.deleted_at IS NULL
               ORDER BY pc.started_at DESC
               LIMIT ?""", (limit,))
        return [dict(r) for r in rows] if rows else []

    async def detail_with_contact(self, id_or_sid: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            """SELECT pc.id, pc.call_sid, pc.phone_number, pc.direction, pc.status,
                      pc.agenda, pc.exchange_count, pc.duration_seconds, pc.recording_path,
                      pc.started_at, pc.completed_at, pc.transcript, pc.outcome,
                      c.id as contact_id, c.name as contact_name
               FROM phone_calls pc
               LEFT JOIN contacts c ON c.phone_number = pc.phone_number AND c.deleted_at IS NULL
               WHERE pc.id = ? OR pc.call_sid = ?""",
            (id_or_sid, id_or_sid))
        return dict(row) if row else None

    async def latest_for_subagent(self, subagent_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM phone_calls WHERE subagent_id = ? ORDER BY started_at DESC LIMIT 1",
            (subagent_id,))
        return dict(row) if row else None

    async def outcome_with_call_session(self, call_id: str) -> dict[str, Any] | None:
        """Call outcome plus the subagent's session key (voice-as-binding)."""
        row = await self.db.fetch_one(
            """SELECT p.subagent_id, p.duration_seconds, p.outcome, s.session_key
               FROM phone_calls p LEFT JOIN subagents s ON s.id = p.subagent_id
               WHERE p.id = ?""", (call_id,))
        return dict(row) if row else None

    async def completed_before(self, cutoff_iso: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT id, recording_path FROM phone_calls WHERE completed_at < ?",
            (cutoff_iso,))
        return [dict(r) for r in rows] if rows else []

    # ---------------------------------------------------------- lifecycle

    async def claim_result_dispatch(self, call_id: str) -> bool:
        """Atomically claim the one-shot result dispatch for a call.
        True for exactly one caller; safe across restarts and races."""
        claimed = await self.db.execute(
            "UPDATE phone_calls SET result_dispatched_at = datetime('now') "
            "WHERE id = ? AND result_dispatched_at IS NULL",
            (call_id,))
        return bool(claimed)

    async def set_status_by_sid(self, call_sid: str, status: str) -> None:
        await self.db.execute(
            "UPDATE phone_calls SET status = ? WHERE call_sid = ?",
            (status, call_sid))

    async def complete_by_sid(
        self, call_sid: str, status: str, *, duration_seconds: int | None = None,
    ) -> None:
        if duration_seconds is not None:
            await self.db.execute(
                """UPDATE phone_calls
                   SET status = ?, completed_at = datetime('now'), duration_seconds = ?
                   WHERE call_sid = ?""",
                (status, duration_seconds, call_sid))
        else:
            await self.db.execute(
                """UPDATE phone_calls
                   SET status = ?, completed_at = datetime('now')
                   WHERE call_sid = ?""",
                (status, call_sid))

    async def attach_stream(self, call_id: str, stream_sid: str) -> None:
        await self.db.execute(
            "UPDATE phone_calls SET stream_sid = ?, status = 'active' WHERE id = ?",
            (stream_sid, call_id))

    async def set_transcript(self, call_id: str, transcript: str) -> None:
        await self.db.execute(
            "UPDATE phone_calls SET transcript = ? WHERE id = ?",
            (transcript, call_id))

    async def finalize(
        self,
        call_id: str,
        *,
        transcript: str,
        recording_path: str | None,
        duration_seconds: int | None,
        outcome_json: str | None,
    ) -> None:
        await self.db.execute(
            """UPDATE phone_calls
               SET status = 'completed', completed_at = datetime('now'),
                   transcript = ?, recording_path = ?, duration_seconds = ?, outcome = ?
               WHERE id = ?""",
            (transcript, recording_path, duration_seconds, outcome_json, call_id))

    async def delete(self, call_id: str) -> None:
        await self.db.execute("DELETE FROM phone_calls WHERE id = ?", (call_id,))

    # ------------------------------------------------- voice-link mirrors

    async def activate_voice_link(self, token: str) -> None:
        await self.db.execute(
            "UPDATE phone_calls SET status='active' WHERE id = ? AND direction='voice_link'",
            (token,))

    async def set_voice_link_transcript(self, token: str, transcript: str) -> None:
        await self.db.execute(
            "UPDATE phone_calls SET transcript = ? WHERE id = ? AND direction='voice_link'",
            (transcript, token))

    async def complete_voice_link(
        self,
        token: str,
        *,
        transcript: str,
        duration_seconds: float,
        outcome_json: str | None,
    ) -> None:
        await self.db.execute(
            """UPDATE phone_calls
               SET status='completed', transcript=?, duration_seconds=?, outcome=?, completed_at=datetime('now')
               WHERE id=? AND direction='voice_link'""",
            (transcript, duration_seconds, outcome_json, token))

    async def cancel_voice_links_for_subagent(self, subagent_id: str, now_iso: str) -> None:
        await self.db.execute(
            "UPDATE phone_calls SET status = 'canceled', completed_at = ? "
            "WHERE subagent_id = ? AND direction = 'voice_link' AND status IN ('ringing', 'active')",
            (now_iso, subagent_id))

    async def complete_stale_voice_links(self) -> None:
        """Active voice-link mirrors are dead after a restart."""
        await self.db.execute(
            """UPDATE phone_calls
               SET status='completed', completed_at=datetime('now')
               WHERE direction='voice_link' AND status='active'""")

    async def expire_stale_voice_links(self, ttl_hours: int) -> None:
        """Never-tapped links would show as 'ringing' forever."""
        await self.db.execute(
            "UPDATE phone_calls SET status='canceled', completed_at=datetime('now') "
            "WHERE direction='voice_link' AND status='ringing' AND started_at < datetime('now', ?)",
            (f"-{ttl_hours} hours",))
