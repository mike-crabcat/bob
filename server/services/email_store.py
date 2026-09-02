"""Email domain store — owns all email_inboxes / email_threads / email_messages SQL.

Local mirror of AgentMail state: inboxes we poll, threads we track (each
bound to a session), and messages (both directions; outbound rows carry the
inbox's own address as sender). ``thread_id`` on email_messages is the
AgentMail thread id, not the local email_threads.id.
"""

from __future__ import annotations

from typing import Any

from server.services.base import utcnow


class EmailStore:
    def __init__(self, db: Any):
        self.db = db

    # ------------------------------------------------------------- inboxes

    async def insert_inbox(
        self, *, inbox_id: str, agentmail_inbox_id: str, display_name: str | None,
        email_address: str, metadata_json: str, now_iso: str,
    ) -> None:
        await self.db.execute(
            """INSERT INTO email_inboxes
               (id, agentmail_inbox_id, display_name, email_address, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (inbox_id, agentmail_inbox_id, display_name, email_address,
             metadata_json, now_iso, now_iso))

    async def get_inbox(
        self, inbox_id: str, *, include_deleted: bool = False, active_only: bool = False,
    ) -> dict[str, Any] | None:
        q = "SELECT * FROM email_inboxes WHERE id = ?"
        if not include_deleted:
            q += " AND deleted_at IS NULL"
        if active_only:
            q += " AND is_active = 1"
        row = await self.db.fetch_one(q, (inbox_id,))
        return dict(row) if row else None

    async def inbox_by_agentmail_id(self, agentmail_inbox_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM email_inboxes WHERE agentmail_inbox_id = ? AND deleted_at IS NULL",
            (agentmail_inbox_id,))
        return dict(row) if row else None

    async def list_inboxes(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        q = "SELECT * FROM email_inboxes WHERE deleted_at IS NULL"
        if active_only:
            q += " AND is_active = 1"
        q += " ORDER BY created_at ASC"
        rows = await self.db.fetch_all(q)
        return [dict(r) for r in rows] if rows else []

    async def active_inboxes(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM email_inboxes WHERE deleted_at IS NULL AND is_active = 1")
        return [dict(r) for r in rows] if rows else []

    async def first_active_inbox(self) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM email_inboxes WHERE deleted_at IS NULL AND is_active = 1 LIMIT 1")
        return dict(row) if row else None

    _INBOX_UPDATABLE = {"display_name", "email_address", "metadata", "is_active", "updated_at"}

    async def update_inbox_fields(self, inbox_id: str, values: dict[str, Any]) -> None:
        bad = set(values) - self._INBOX_UPDATABLE
        if bad:
            raise ValueError(f"non-updatable email_inboxes columns: {sorted(bad)}")
        if not values:
            return
        values = dict(values)
        values.setdefault("updated_at", utcnow().isoformat())
        assignments = ", ".join(f"{field} = ?" for field in values)
        # Column names come from the whitelist above, never caller input.
        await self.db.execute(
            f"UPDATE email_inboxes SET {assignments} WHERE id = ?",
            tuple(values.values()) + (inbox_id,))

    async def soft_delete_inbox(self, inbox_id: str, now_iso: str) -> None:
        await self.db.execute(
            "UPDATE email_inboxes SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (now_iso, now_iso, inbox_id))

    async def mark_polled(self, inbox_id: str, now_iso: str) -> None:
        await self.db.execute(
            "UPDATE email_inboxes SET last_polled_at = ?, updated_at = ? WHERE id = ?",
            (now_iso, now_iso, inbox_id))

    # ------------------------------------------------------------- threads

    async def thread_by_agentmail(
        self, inbox_id: str, agentmail_thread_id: str,
    ) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM email_threads WHERE inbox_id = ? AND agentmail_thread_id = ? AND deleted_at IS NULL",
            (inbox_id, agentmail_thread_id))
        return dict(row) if row else None

    async def thread_by_agentmail_any(self, agentmail_thread_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM email_threads WHERE agentmail_thread_id = ? AND deleted_at IS NULL",
            (agentmail_thread_id,))
        return dict(row) if row else None

    async def get_thread(
        self, thread_id: str, *, include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        q = "SELECT * FROM email_threads WHERE id = ?"
        if not include_deleted:
            q += " AND deleted_at IS NULL"
        row = await self.db.fetch_one(q, (thread_id,))
        return dict(row) if row else None

    async def thread_by_id_or_agentmail(self, id_or_agentmail: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM email_threads WHERE id = ? OR agentmail_thread_id = ?",
            (id_or_agentmail, id_or_agentmail))
        return dict(row) if row else None

    async def list_threads(
        self, *, inbox_id: str | None = None, active_only: bool = True,
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM email_threads WHERE deleted_at IS NULL"
        params: list[Any] = []
        if inbox_id is not None:
            q += " AND inbox_id = ?"
            params.append(inbox_id)
        if active_only:
            q += " AND is_active = 1"
        q += " ORDER BY last_message_at DESC NULLS LAST"
        rows = await self.db.fetch_all(q, tuple(params))
        return [dict(r) for r in rows] if rows else []

    async def insert_thread(
        self, *, thread_id: str, inbox_id: str, agentmail_thread_id: str,
        subject: str | None, contact_id: str | None, session_key: str,
        agenda: str | None, origin_session_key: str | None,
        last_message_at: str, now_iso: str,
    ) -> None:
        await self.db.execute(
            """INSERT INTO email_threads (
                   id, inbox_id, agentmail_thread_id, subject,
                   contact_id, session_key, agenda, origin_session_key,
                   message_count, last_message_at, is_active,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1, ?, ?)""",
            (thread_id, inbox_id, agentmail_thread_id, subject,
             contact_id, session_key, agenda, origin_session_key,
             last_message_at, now_iso, now_iso))

    async def set_thread_origin(self, thread_id: str, origin_session_key: str) -> None:
        await self.db.execute(
            "UPDATE email_threads SET origin_session_key = ? WHERE id = ?",
            (origin_session_key, thread_id))

    async def clear_thread_origin(self, id_or_agentmail: str) -> None:
        await self.db.execute(
            "UPDATE email_threads SET origin_session_key = NULL WHERE id = ? OR agentmail_thread_id = ?",
            (id_or_agentmail, id_or_agentmail))

    async def set_thread_agenda(self, thread_id: str, agenda: str, now_iso: str) -> None:
        await self.db.execute(
            "UPDATE email_threads SET agenda = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (agenda, now_iso, thread_id))

    async def bump_thread_stats(self, thread_id: str, now_iso: str) -> None:
        await self.db.execute(
            """UPDATE email_threads
               SET message_count = message_count + 1, last_message_at = ?, updated_at = ?
               WHERE id = ?""",
            (now_iso, now_iso, thread_id))

    async def bump_thread_stats_by_agentmail(self, agentmail_thread_id: str, now_iso: str) -> None:
        await self.db.execute(
            """UPDATE email_threads
               SET message_count = message_count + 1, last_message_at = ?, updated_at = ?
               WHERE agentmail_thread_id = ? AND deleted_at IS NULL""",
            (now_iso, now_iso, agentmail_thread_id))

    async def recount_inbox_threads(self, inbox_id: str, now_iso: str) -> None:
        """Recompute message_count/last_message_at from stored messages (backfill)."""
        await self.db.execute(
            """UPDATE email_threads
               SET message_count = (
                   SELECT COUNT(*) FROM email_messages em
                   WHERE em.thread_id = email_threads.agentmail_thread_id
               ),
               last_message_at = (
                   SELECT MAX(em.message_timestamp) FROM email_messages em
                   WHERE em.thread_id = email_threads.agentmail_thread_id
               ),
               updated_at = ?
               WHERE inbox_id = ? AND deleted_at IS NULL""",
            (now_iso, inbox_id))

    async def agenda_for_session(self, session_key: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT agenda FROM email_threads WHERE session_key = ? AND agenda IS NOT NULL AND agenda != '' AND deleted_at IS NULL LIMIT 1",
            (session_key,))
        return row["agenda"] if row else None

    async def search_threads(
        self, terms: list[str], *, contact_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Rank active threads by subject/body keyword matches (contacts join
        is a sanctioned cross-domain read for display names)."""
        if not terms:
            return []
        msg_conditions = " OR ".join(
            "(em.text_body LIKE ? OR em.subject LIKE ?)" for _ in terms)
        msg_params: list[str] = []
        for t in terms:
            like = f"%{t}%"
            msg_params.extend([like, like])
        scope_clause = "AND et.contact_id = ?" if contact_id else ""
        scope_params = [contact_id] if contact_id else []
        sql = (
            "SELECT et.agentmail_thread_id, et.subject, et.contact_id, "
            "et.message_count, et.last_message_at, "
            "c.name as contact_name, "
            "COUNT(DISTINCT em.id) as matching_messages, "
            "CASE WHEN et.subject LIKE ? THEN 1 ELSE 0 END as subject_match "
            "FROM email_threads et "
            "LEFT JOIN email_messages em ON em.thread_id = et.agentmail_thread_id "
            f"AND ({msg_conditions}) "
            "LEFT JOIN contacts c ON c.id = et.contact_id "
            f"WHERE et.deleted_at IS NULL AND et.is_active = 1 {scope_clause} "
            "GROUP BY et.agentmail_thread_id "
            "HAVING matching_messages > 0 OR subject_match = 1 "
            "ORDER BY subject_match DESC, matching_messages DESC, et.last_message_at DESC "
            "LIMIT 20"
        )
        params = [f"%{terms[0]}%"] + msg_params + scope_params
        rows = await self.db.fetch_all(sql, tuple(params))
        return [dict(r) for r in rows] if rows else []

    # ------------------------------------------------------------ messages

    async def message_by_agentmail_id(self, agentmail_message_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM email_messages WHERE agentmail_message_id = ?",
            (agentmail_message_id,))
        return dict(row) if row else None

    async def insert_message(
        self, *, message_id: str, inbox_id: str, agentmail_message_id: str,
        thread_id: str, subject: str | None, sender_email: str | None,
        sender_name: str | None, to_addresses_json: str, cc_addresses_json: str,
        text_body: str | None, html_body: str | None, preview: str | None,
        labels_json: str, has_attachments: bool, in_reply_to: str | None,
        message_timestamp: str, now_iso: str, txn: Any = None,
    ) -> None:
        conn = txn if txn is not None else self.db
        await conn.execute(
            """INSERT INTO email_messages (
                   id, inbox_id, agentmail_message_id, thread_id,
                   subject, sender_email, sender_name,
                   to_addresses, cc_addresses,
                   text_body, html_body, preview, labels,
                   has_attachments, in_reply_to,
                   message_timestamp, processed_at, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, inbox_id, agentmail_message_id, thread_id,
             subject, sender_email, sender_name,
             to_addresses_json, cc_addresses_json,
             text_body, html_body, preview, labels_json,
             1 if has_attachments else 0, in_reply_to,
             message_timestamp, now_iso, now_iso))

    async def set_attachments_json(self, message_id: str, attachments_json: str) -> None:
        await self.db.execute(
            "UPDATE email_messages SET attachments_json = ? WHERE id = ?",
            (attachments_json, message_id))

    async def set_attachments_json_by_agentmail(
        self, agentmail_message_id: str, attachments_json: str,
    ) -> None:
        await self.db.execute(
            "UPDATE email_messages SET attachments_json = ? WHERE agentmail_message_id = ?",
            (attachments_json, agentmail_message_id))

    async def attachments_json_of(self, agentmail_message_id: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT attachments_json FROM email_messages WHERE agentmail_message_id = ?",
            (agentmail_message_id,))
        return row["attachments_json"] if row else None

    async def first_from_sender(
        self, agentmail_thread_id: str, sender_email: str,
    ) -> dict[str, Any] | None:
        """Earliest message from a given sender in a thread (e.g. our own
        outbound email that opened it)."""
        row = await self.db.fetch_one(
            """SELECT text_body, subject FROM email_messages
               WHERE thread_id = ? AND sender_email = ?
               ORDER BY message_timestamp ASC LIMIT 1""",
            (agentmail_thread_id, sender_email))
        return dict(row) if row else None

    async def latest_in_thread(self, agentmail_thread_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            """SELECT agentmail_message_id, inbox_id FROM email_messages
               WHERE thread_id = ?
               ORDER BY message_timestamp DESC LIMIT 1""",
            (agentmail_thread_id,))
        return dict(row) if row else None

    async def attachment_messages(self, agentmail_thread_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT agentmail_message_id, sender_email, sender_name,
                      subject, message_timestamp, attachments_json
               FROM email_messages
               WHERE thread_id = ? AND has_attachments = 1
               ORDER BY message_timestamp ASC""",
            (agentmail_thread_id,))
        return [dict(r) for r in rows] if rows else []

    async def thread_messages(self, agentmail_thread_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT sender_email, sender_name, subject, text_body, message_timestamp
               FROM email_messages WHERE thread_id = ?
               ORDER BY message_timestamp ASC""",
            (agentmail_thread_id,))
        return [dict(r) for r in rows] if rows else []

    async def thread_participants(self, agentmail_thread_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT DISTINCT sender_email, sender_name FROM email_messages em
               INNER JOIN email_threads et ON et.id = em.thread_id
               WHERE et.agentmail_thread_id = ? ORDER BY em.message_timestamp ASC""",
            (agentmail_thread_id,))
        return [dict(r) for r in rows] if rows else []

    async def inbound_count_since(self, since_iso: str) -> int:
        """Received (non-self-sent) messages since a timestamp — event_log
        reconciliation audit."""
        row = await self.db.fetch_one(
            """SELECT COUNT(*) AS n FROM email_messages m
               JOIN email_inboxes i ON i.id = m.inbox_id
               WHERE m.created_at >= ? AND m.sender_email != i.email_address""",
            (since_iso,))
        return (row or {}).get("n", 0) or 0
