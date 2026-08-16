"""DreamStore — persistence for dream runs, resolutions, plans, cursors, links."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from bob_server.context import AppContext
from bob_server.database import Database
from bob_server.services.base import BaseService, iso_utc, json_dumps, json_loads, utcnow
from bob_server.services.dream.models import (
    PLAN_ACTIVE_STATUSES,
    PLAN_TERMINAL_STATUSES,
    RESOLUTION_ACTIVE_STATUSES,
    RESOLUTION_TERMINAL_STATUSES,
    Evidence,
    PlanCandidate,
    ResolutionCandidate,
)

logger = logging.getLogger(__name__)


def _hex8() -> str:
    return secrets.token_hex(4)


def new_run_id() -> str:
    return f"dream-{utcnow().strftime('%Y-%m-%d')}-{_hex8()}"


def new_item_id(item_type: str) -> str:
    return f"{item_type}-{_hex8()}"


class DreamStore(BaseService):
    """All SQL for the dream system. Writes only dream_* tables."""

    # ------------------------------------------------------------------ runs

    async def create_run(self, *, trigger: str, window_start: str, window_end: str, model: str) -> str:
        run_id = new_run_id()
        await self.db.execute(
            "INSERT INTO dream_runs (id, started_at, window_start, window_end, status, trigger, model) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (run_id, iso_utc(), window_start, window_end, trigger, model),
        )
        return run_id

    async def finish_run(self, run_id: str, *, stats: dict, journal_text: str) -> None:
        await self.db.execute(
            "UPDATE dream_runs SET finished_at = ?, status = 'complete', stats_json = ?, journal_text = ? "
            "WHERE id = ?",
            (iso_utc(), json_dumps(stats) or "{}", journal_text, run_id),
        )

    async def fail_run(self, run_id: str, error: str) -> None:
        await self.db.execute(
            "UPDATE dream_runs SET finished_at = ?, status = 'failed', error = ? WHERE id = ?",
            (iso_utc(), error[:2000], run_id),
        )

    async def update_run_stats(self, run_id: str, stats: dict) -> None:
        await self.db.execute(
            "UPDATE dream_runs SET stats_json = ? WHERE id = ?",
            (json_dumps(stats) or "{}", run_id),
        )

    async def get_run(self, run_id: str) -> dict | None:
        return await self.db.fetch_one("SELECT * FROM dream_runs WHERE id = ?", (run_id,))

    async def list_runs(self, limit: int = 20) -> list[dict]:
        rows = await self.db.fetch_all(
            "SELECT * FROM dream_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return rows if rows else []

    async def last_complete_run(self) -> dict | None:
        return await self.db.fetch_one(
            "SELECT * FROM dream_runs WHERE status = 'complete' ORDER BY finished_at DESC LIMIT 1"
        )

    async def sweep_stale_runs(self, stale_minutes: int = 60) -> int:
        """Mark runs stuck in 'running' as failed (startup crash-safety)."""
        return await self.db.execute(
            "UPDATE dream_runs SET finished_at = ?, status = 'failed', "
            "error = COALESCE(error, 'stale running dream — swept') "
            "WHERE status = 'running' AND datetime(started_at) < datetime('now', ?)",
            (iso_utc(), f"-{stale_minutes} minutes"),
        )

    async def count_complete_runs_since(self, item_created_before: str) -> int:
        """Complete runs since a given timestamp — used for stall counters."""
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM dream_runs WHERE status = 'complete' "
            "AND datetime(finished_at) > datetime(?)",
            (item_created_before,),
        )
        return int(row["n"]) if row else 0

    # -------------------------------------------------------------- cursors

    async def get_cursor(self, session_key: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT last_reviewed_message_at FROM dream_session_review WHERE session_key = ?",
            (session_key,),
        )
        return row["last_reviewed_message_at"] if row else None

    async def set_cursor(self, session_key: str, message_at: str, run_id: str) -> None:
        await self.db.execute(
            "INSERT INTO dream_session_review (session_key, last_reviewed_message_at, run_id, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_key) DO UPDATE SET "
            "last_reviewed_message_at = excluded.last_reviewed_message_at, "
            "run_id = excluded.run_id, updated_at = excluded.updated_at",
            (session_key, message_at, run_id, iso_utc()),
        )

    async def sessions_due(
        self,
        *,
        min_new_messages: int,
        max_sessions: int,
        first_run_lookback_days: int,
    ) -> list[dict]:
        """Sessions with unreviewed messages, newest activity first.

        First sight of a session is bounded by the lookback so run #1 doesn't
        queue months of history; once a cursor exists, any messages after it
        count (they're leftovers the dream owes a review).
        """
        rows = await self.db.fetch_all(
            """
            SELECT
                sm.session_key,
                MAX(sm.created_at) AS newest_message_at,
                COUNT(*) AS new_messages,
                COALESCE(dsr.last_reviewed_message_at, '') AS cursor_at
            FROM session_messages sm
            LEFT JOIN dream_session_review dsr ON dsr.session_key = sm.session_key
            WHERE sm.session_key NOT LIKE 'subagent:%'
              AND datetime(sm.created_at) > datetime(COALESCE(dsr.last_reviewed_message_at, '1970-01-01'))
              AND (
                dsr.session_key IS NOT NULL
                OR datetime(sm.created_at) > datetime('now', ?)
              )
            GROUP BY sm.session_key
            HAVING COUNT(*) >= ?
            ORDER BY newest_message_at DESC
            LIMIT ?
            """,
            (f"-{first_run_lookback_days} days", min_new_messages, max_sessions),
        )
        return [dict(r) for r in rows] if rows else []

    async def fetch_session_window(
        self, session_key: str, cursor_at: str | None, *, lookback_days: int, limit: int
    ) -> list[dict]:
        """Messages to review: after the cursor (or within lookback on first sight)."""
        if cursor_at:
            rows = await self.db.fetch_all(
                "SELECT created_at, role, sender_id, content FROM session_messages "
                "WHERE session_key = ? AND datetime(created_at) > datetime(?) "
                "ORDER BY created_at ASC LIMIT ?",
                (session_key, cursor_at, limit),
            )
        else:
            rows = await self.db.fetch_all(
                "SELECT created_at, role, sender_id, content FROM session_messages "
                "WHERE session_key = ? AND datetime(created_at) > datetime('now', ?) "
                "ORDER BY created_at ASC LIMIT ?",
                (session_key, f"-{lookback_days} days", limit),
            )
        return [dict(r) for r in rows] if rows else []

    # ------------------------------------------------------------ resolutions

    async def defer_candidate(
        self, item_type: str, session_key: str, candidate: dict, *, run_id: str,
        max_queue: int = 30,
    ) -> None:
        """Persist a capped candidate for the next run to process first."""
        await self.db.execute(
            "INSERT INTO dream_deferred_candidates (item_type, session_key, candidate_json, created_at, source_run_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (item_type, session_key, json_dumps(candidate) or "{}", iso_utc(), run_id),
        )
        # Bound the queue: drop the oldest beyond max_queue.
        await self.db.execute(
            "DELETE FROM dream_deferred_candidates WHERE id IN ("
            "  SELECT id FROM dream_deferred_candidates ORDER BY id DESC LIMIT -1 OFFSET ?)",
            (max_queue,),
        )

    async def load_deferred(self) -> list[dict]:
        rows = await self.db.fetch_all(
            "SELECT id, item_type, session_key, candidate_json FROM dream_deferred_candidates ORDER BY id ASC"
        )
        out = []
        for r in rows or []:
            candidate = json_loads(r["candidate_json"], {})
            if not isinstance(candidate, dict):
                continue
            out.append({
                "deferred_id": r["id"],
                "item_type": r["item_type"],
                "session_key": r["session_key"] or "",
                "candidate": candidate,
            })
        return out

    async def delete_deferred(self, deferred_ids: list[int]) -> None:
        if not deferred_ids:
            return
        marks = ",".join("?" for _ in deferred_ids)
        await self.db.execute(
            f"DELETE FROM dream_deferred_candidates WHERE id IN ({marks})", tuple(deferred_ids)
        )

    async def insert_resolution(
        self, cand: ResolutionCandidate, *, run_id: str, status: str
    ) -> str:
        item_id = new_item_id("resolution")
        now = iso_utc()
        await self.db.execute(
            """INSERT INTO dream_resolutions
               (id, title, behaviour, trigger_condition, success_signal, status,
                first_seen_at, last_seen_at, observation_count, evidence_json, source_run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                item_id, cand.title, cand.behaviour, cand.trigger_condition,
                cand.success_signal, status, now, now,
                json_dumps([e.to_dict() for e in cand.evidence]) or "[]", run_id,
            ),
        )
        return item_id

    async def merge_resolution(self, item_id: str, evidence: list[Evidence], *, run_id: str) -> None:
        row = await self.db.fetch_one(
            "SELECT evidence_json FROM dream_resolutions WHERE id = ?", (item_id,)
        )
        existing = json_loads(row["evidence_json"] if row else None, [])
        existing.extend(e.to_dict() for e in evidence)
        await self.db.execute(
            "UPDATE dream_resolutions SET last_seen_at = ?, observation_count = observation_count + 1, "
            "evidence_json = ? WHERE id = ?",
            (iso_utc(), json_dumps(existing) or "[]", item_id),
        )

    async def set_resolution_status(self, item_id: str, status: str, *, evidence: Evidence | None = None) -> None:
        if evidence is not None:
            await self._append_resolution_evidence(item_id, evidence)
        await self.db.execute(
            "UPDATE dream_resolutions SET status = ? WHERE id = ?", (status, item_id)
        )

    async def _append_resolution_evidence(self, item_id: str, evidence: Evidence) -> None:
        row = await self.db.fetch_one(
            "SELECT evidence_json FROM dream_resolutions WHERE id = ?", (item_id,)
        )
        existing = json_loads(row["evidence_json"] if row else None, [])
        existing.append(evidence.to_dict())
        await self.db.execute(
            "UPDATE dream_resolutions SET evidence_json = ? WHERE id = ?",
            (json_dumps(existing) or "[]", item_id),
        )

    async def list_resolutions(self, statuses: list[str] | None = None, limit: int = 200) -> list[dict]:
        if statuses:
            marks = ",".join("?" for _ in statuses)
            rows = await self.db.fetch_all(
                f"SELECT * FROM dream_resolutions WHERE status IN ({marks}) "
                "ORDER BY last_seen_at DESC LIMIT ?",
                (*statuses, limit),
            )
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM dream_resolutions ORDER BY last_seen_at DESC LIMIT ?", (limit,)
            )
        return rows if rows else []

    # ------------------------------------------------------------------ plans

    async def insert_plan(
        self, cand: PlanCandidate, *, run_id: str, status: str,
        approved_by: str | None = None, session_key: str = "",
    ) -> str:
        item_id = new_item_id("plan")
        now = iso_utc()
        await self.db.execute(
            """INSERT INTO dream_plans
               (id, title, what_was_discussed, proposed_action, assistance_method,
                autonomy_tier, status, approved_by, approved_at, evidence_json,
                source_run_id, due_hint, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item_id, cand.title, cand.what_was_discussed, cand.proposed_action,
                cand.assistance_method, cand.autonomy_tier, status, approved_by,
                iso_utc() if approved_by else None,
                json_dumps([e.to_dict() for e in cand.evidence]) or "[]",
                run_id, cand.due_hint or None, now, now,
            ),
        )
        for entity_id in cand.related_entities:
            await self.add_link("plan", item_id, session_key=session_key, entity_id=entity_id)
        if session_key:
            await self.add_link("plan", item_id, session_key=session_key)
        return item_id

    async def merge_plan(self, item_id: str, evidence: list[Evidence], *, run_id: str) -> None:
        # Plans don't carry observation_count; merging appends evidence and refreshes timestamp.
        row = await self.db.fetch_one("SELECT evidence_json FROM dream_plans WHERE id = ?", (item_id,))
        existing = json_loads(row["evidence_json"] if row else None, [])
        existing.extend(e.to_dict() for e in evidence)
        await self.db.execute(
            "UPDATE dream_plans SET evidence_json = ?, updated_at = ? WHERE id = ?",
            (json_dumps(existing) or "[]", iso_utc(), item_id),
        )

    async def get_plan(self, plan_id: str) -> dict | None:
        return await self.db.fetch_one("SELECT * FROM dream_plans WHERE id = ?", (plan_id,))

    async def update_plan_fields(
        self, plan_id: str, *, due_hint: str | None = None, proposed_action: str | None = None,
        assistance_method: str | None = None,
    ) -> None:
        sets, params = ["updated_at = ?"], [iso_utc()]
        if due_hint is not None:
            sets.append("due_hint = ?")
            params.append(due_hint)
        if proposed_action is not None:
            sets.append("proposed_action = ?")
            params.append(proposed_action)
        if assistance_method is not None:
            sets.append("assistance_method = ?")
            params.append(assistance_method)
        params.append(plan_id)
        await self.db.execute(f"UPDATE dream_plans SET {', '.join(sets)} WHERE id = ?", tuple(params))

    async def append_plan_evidence(self, plan_id: str, evidence: Evidence) -> None:
        row = await self.db.fetch_one("SELECT evidence_json FROM dream_plans WHERE id = ?", (plan_id,))
        existing = json_loads(row["evidence_json"] if row else None, [])
        existing.append(evidence.to_dict())
        await self.db.execute(
            "UPDATE dream_plans SET evidence_json = ?, updated_at = ? WHERE id = ?",
            (json_dumps(existing) or "[]", iso_utc(), plan_id),
        )

    async def set_plan_status(
        self, plan_id: str, status: str, *, approved_by: str | None = None,
        evidence: Evidence | None = None,
    ) -> None:
        if evidence is not None:
            await self.append_plan_evidence(plan_id, evidence)
        if approved_by is not None:
            await self.db.execute(
                "UPDATE dream_plans SET status = ?, approved_by = ?, approved_at = ?, updated_at = ? "
                "WHERE id = ?",
                (status, approved_by, iso_utc(), iso_utc(), plan_id),
            )
        else:
            await self.db.execute(
                "UPDATE dream_plans SET status = ?, updated_at = ? WHERE id = ?",
                (status, iso_utc(), plan_id),
            )

    async def list_plans(self, statuses: list[str] | None = None, limit: int = 200) -> list[dict]:
        if statuses:
            marks = ",".join("?" for _ in statuses)
            rows = await self.db.fetch_all(
                f"SELECT * FROM dream_plans WHERE status IN ({marks}) "
                "ORDER BY updated_at DESC LIMIT ?",
                (*statuses, limit),
            )
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM dream_plans ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
        return rows if rows else []

    # ----------------------------------------------------------------- links

    async def add_link(
        self, item_type: str, item_id: str, *, session_key: str | None = None, entity_id: str | None = None
    ) -> None:
        if not session_key and not entity_id:
            return
        await self.db.execute(
            "INSERT INTO dream_item_links (item_type, item_id, session_key, entity_id) VALUES (?, ?, ?, ?)",
            (item_type, item_id, session_key, entity_id),
        )

    async def links_for_item(self, item_type: str, item_id: str) -> list[dict]:
        rows = await self.db.fetch_all(
            "SELECT * FROM dream_item_links WHERE item_type = ? AND item_id = ?",
            (item_type, item_id),
        )
        return rows if rows else []

    async def items_for_session(self, session_key: str, *, item_type: str | None = None) -> list[dict]:
        """Items linked to a session — powers the Tier 1 injection and plan tools."""
        if item_type:
            rows = await self.db.fetch_all(
                "SELECT * FROM dream_item_links WHERE session_key = ? AND item_type = ?",
                (session_key, item_type),
            )
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM dream_item_links WHERE session_key = ?", (session_key,)
            )
        return rows if rows else []

    async def items_for_entity(self, entity_id: str, *, statuses: list[str] | None = None) -> list[dict]:
        """Open items linked to an entity (person/group) — recall augmentation."""
        rows = await self.db.fetch_all(
            "SELECT item_type, item_id FROM dream_item_links WHERE entity_id = ?", (entity_id,)
        )
        results: list[dict] = []
        for r in rows or []:
            table = "dream_plans" if r["item_type"] == "plan" else "dream_resolutions"
            marks = ",".join("?" for _ in statuses) if statuses else None
            if marks:
                row = await self.db.fetch_one(
                    f"SELECT * FROM {table} WHERE id = ? AND status IN ({marks})",
                    (r["item_id"], *statuses),
                )
            else:
                row = await self.db.fetch_one(f"SELECT * FROM {table} WHERE id = ?", (r["item_id"],))
            if row:
                results.append(dict(row) | {"item_type": r["item_type"]})
        return results

    # ------------------------------------------------------------ embeddings

    async def upsert_item_embedding(self, item_id: str, embedding: list[float]) -> None:
        from bob_server.services.memory.embedding import _pack_embedding

        await self.db.execute(
            "DELETE FROM dream_item_embeddings WHERE item_id = ?", (item_id,)
        )
        await self.db.execute(
            "INSERT INTO dream_item_embeddings(item_id, embedding) VALUES (?, ?)",
            (item_id, _pack_embedding(embedding)),
        )

    async def rebuild_item_embeddings(self) -> int:
        """Re-embed all non-terminal items (title + body). Used after metric changes."""
        from bob_server.services.memory.embedding import embed_batch

        resolutions = await self.list_resolutions(list(RESOLUTION_ACTIVE_STATUSES) + ["draft"])
        plans = await self.list_plans(list(PLAN_ACTIVE_STATUSES))
        texts, keys = [], []
        for r in resolutions:
            texts.append(f"{r['title']}\n{r['behaviour']}"[:2000])
            keys.append(r["id"])
        for p in plans:
            texts.append(f"{p['title']}\n{p['what_was_discussed']}"[:2000])
            keys.append(p["id"])
        if not texts:
            return 0
        vectors = await embed_batch(texts)
        count = 0
        for item_id, vec in zip(keys, vectors):
            if vec is not None:
                await self.upsert_item_embedding(item_id, vec)
                count += 1
        return count

    async def find_similar_items(
        self, query_embedding: list[float], *, threshold: float, limit: int = 5
    ) -> list[dict]:
        """Cosine-similar existing items: [{item_id, distance}]."""
        from bob_server.services.memory.embedding import _pack_embedding

        try:
            rows = await self.db.fetch_all(
                "SELECT item_id, distance FROM dream_item_embeddings "
                "WHERE embedding MATCH ? AND distance < ? ORDER BY distance LIMIT ?",
                (_pack_embedding(query_embedding), threshold, limit),
            )
        except Exception:
            logger.warning("dream embedding search failed", exc_info=True)
            return []
        return [dict(r) for r in rows] if rows else []

    async def _load_items_by_ids(self, item_type: str, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        table = "dream_plans" if item_type == "plan" else "dream_resolutions"
        marks = ",".join("?" for _ in ids)
        rows = await self.db.fetch_all(
            f"SELECT * FROM {table} WHERE id IN ({marks})", tuple(ids)
        )
        return rows if rows else []

    async def dedup_targets(
        self, item_type: str, query_embedding: list[float], *, threshold: float,
        terminal_within_days: int,
    ) -> tuple[dict | None, dict | None]:
        """Find a match among active items, and among recently-terminal items.

        Returns (active_match, terminal_match) — rows with distance included,
        or None. A terminal match within the window suppresses re-creation;
        one with fresh explicit evidence may instead be reopened in place.
        """
        hits = await self.find_similar_items(query_embedding, threshold=threshold, limit=10)
        if not hits:
            return None, None
        plan_ids = [h["item_id"] for h in hits if h["item_id"].startswith("plan-")]
        res_ids = [h["item_id"] for h in hits if h["item_id"].startswith("resolution-")]
        wanted = plan_ids if item_type == "plan" else res_ids
        dist = {h["item_id"]: h["distance"] for h in hits}
        rows = await self._load_items_by_ids(item_type, wanted)
        active_statuses = PLAN_ACTIVE_STATUSES if item_type == "plan" else RESOLUTION_ACTIVE_STATUSES
        terminal_statuses = PLAN_TERMINAL_STATUSES if item_type == "plan" else RESOLUTION_TERMINAL_STATUSES
        cutoff = (utcnow() - timedelta(days=terminal_within_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        active_match, terminal_match = None, None
        for row in rows:
            row = dict(row) | {"distance": dist.get(row["id"], 1.0)}
            ts = row.get("updated_at") or row.get("last_seen_at") or row.get("created_at") or ""
            if row["status"] in active_statuses and active_match is None:
                active_match = row
            elif row["status"] in terminal_statuses and ts >= cutoff and terminal_match is None:
                terminal_match = row
        return active_match, terminal_match

    # ------------------------------------------------------- announcements

    async def plans_pending_announce(self) -> list[dict]:
        """Approved plans not yet announced."""
        rows = await self.db.fetch_all(
            "SELECT * FROM dream_plans WHERE status = 'approved' AND announced_at IS NULL "
            "ORDER BY updated_at ASC"
        )
        return rows if rows else []

    async def plan_evidence_session(self, plan: dict) -> str | None:
        """The session where the plan's evidence was cited — its only announce target."""
        evidence = json_loads(plan.get("evidence_json"), [])
        for entry in evidence:
            if entry.get("session_key"):
                return entry["session_key"]
        return None

    async def session_last_inbound_at(self, session_key: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT MAX(created_at) AS last_at FROM session_messages "
            "WHERE session_key = ? AND role = 'user'",
            (session_key,),
        )
        return row["last_at"] if row else None

    async def announcements_today(self, session_key: str) -> int:
        """Announcement messages recorded in this session today (daily cap)."""
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM session_messages "
            "WHERE session_key = ? AND role = 'assistant' "
            "AND metadata LIKE '%dream_announce%' AND date(created_at) = date('now')",
            (session_key,),
        )
        return int(row["n"]) if row else 0

    async def user_messages_since(self, session_key: str, since_iso: str) -> list[dict]:
        """Engagement check: user messages in a session after a timestamp."""
        rows = await self.db.fetch_all(
            "SELECT created_at, role, sender_id, content FROM session_messages "
            "WHERE session_key = ? AND role = 'user' AND datetime(created_at) > datetime(?) "
            "ORDER BY created_at ASC LIMIT 50",
            (session_key, since_iso),
        )
        return [dict(r) for r in rows] if rows else []

    async def announce_history(self, limit: int = 30) -> list[dict]:
        """Recent announcement records, newest first (Controls tab)."""
        rows = await self.db.fetch_all(
            "SELECT session_key, content, created_at FROM session_messages "
            "WHERE role = 'assistant' AND metadata LIKE '%dream_announce%' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows] if rows else []


ACTIVE_PLAN_FILTER = list(PLAN_ACTIVE_STATUSES)
ACTIVE_RESOLUTION_FILTER = list(RESOLUTION_ACTIVE_STATUSES)
