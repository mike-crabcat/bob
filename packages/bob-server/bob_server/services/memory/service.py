"""MemoryService v7 — claim-centric memory system.

Claims are the source of truth. Entity records are identity-only.
Rendered views are generated from claims via templates (no LLM).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from bob_server.services.base import BaseService, iso_utc, utcnow
from bob_server.services.memory.claim_types import (
    ENTITY_TYPE_REGISTRY,
    detect_entity_type,
    detect_entity_types_in_text,
)
from bob_server.services.memory.models import (
    Claim,
    EntityDocument,
)
from bob_server.services.memory.claim_types import (
    render_entity,
    ENTITY_TYPES,
)
from bob_server.services.memory.claim_service import write_claim

logger = logging.getLogger(__name__)

# Outstanding remember-tool-deferred extraction tasks. Holding references prevents
# the asyncio scheduler from garbage-collecting them before they complete.
_remember_tasks: set[asyncio.Task] = set()


class MemoryService(BaseService):
    """Reads and writes v7 memory via SQLite: claims, entities."""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self._recon_task: asyncio.Task | None = None

    def _schedule_reconciliation(self, entity_ids: list[str]) -> None:
        """Debounced reconciliation — runs 2s after the trigger fires.

        Used by answer_question (after a reconciliation question is answered)
        and any future caller that needs to re-reconcile specific entities.
        """
        if self._recon_task and not self._recon_task.done():
            self._recon_task.cancel()

        async def _run() -> None:
            await asyncio.sleep(2)
            if not entity_ids:
                return
            try:
                from bob_server.services.memory.reconciliation import (
                    reconcile_entity,
                    deprecate_file_entities_without_path,
                    filter_due_for_reconciliation,
                )
                from bob_server.services.llm_dispatch import LLMDispatchService
                llm = LLMDispatchService(self.ctx)

                # Backoff: skip entities reconciled within the min-interval window.
                min_interval = getattr(
                    getattr(self.ctx.settings, "reconciliation", None),
                    "min_interval_hours", 0.0,
                )
                due = await filter_due_for_reconciliation(self.db, entity_ids, min_interval)
                due_set = set(due)
                skipped = [eid for eid in entity_ids if eid not in due_set]
                if skipped:
                    logger.info(
                        "Reconciliation backoff: skipping %d entities (min_interval_hours=%.1f): %s",
                        len(skipped), min_interval, skipped,
                    )

                # Deprecate file entities with no valid file_path
                await deprecate_file_entities_without_path(self.db)

                for eid in due:
                    result = await reconcile_entity(
                        self.db, llm, eid,
                        settings=self.ctx.settings,
                        update_fts_fn=self._update_entity_fts,
                        schedule_reconciliation_fn=self._schedule_reconciliation,
                    )
                    if result.get("operations_applied") or result.get("questions_raised"):
                        logger.info(
                            "Reconciled %s: %d ops, %d questions",
                            eid,
                            len(result.get("operations_applied", [])),
                            len(result.get("questions_raised", [])),
                        )
            except Exception:
                logger.exception("Reconciliation failed")

        self._recon_task = asyncio.create_task(_run())

    async def reconcile_entities(
        self, workspace_dir: Path, *, entity_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """Manually trigger reconciliation for specific or all active entities."""
        from bob_server.services.memory.reconciliation import reconcile_entity, deprecate_file_entities_without_path
        from bob_server.services.llm_dispatch import LLMDispatchService

        llm = LLMDispatchService(self.ctx)

        # Deprecate file entities with no valid file_path
        await deprecate_file_entities_without_path(self.db)

        if entity_ids is None:
            rows = await self.db.fetch_all(
                "SELECT entity_id FROM memory_entities WHERE status = 'active'"
            )
            entity_ids = [r["entity_id"] for r in rows]

        results = []
        for eid in entity_ids:
            result = await reconcile_entity(
                self.db, llm, eid,
                settings=self.ctx.settings,
                update_fts_fn=self._update_entity_fts,
                schedule_reconciliation_fn=self._schedule_reconciliation,
            )
            results.append(result)

        return {
            "entities_checked": len(results),
            "total_issues": sum(len(r.get("issues", [])) for r in results),
            "total_ops": sum(len(r.get("operations_applied", [])) for r in results),
            "total_questions": sum(len(r.get("questions_raised", [])) for r in results),
            "details": results,
        }

    async def answer_question(
        self, workspace_dir: Path, question_id: str, answer: str,
    ) -> dict[str, Any]:
        """Answer a reconciliation question and queue the entity for re-reconciliation."""
        row = await self.db.fetch_one(
            "SELECT id, entity_id, question FROM memory_questions WHERE id = ? AND status = 'open'",
            (question_id,),
        )
        if not row:
            return {"status": "not_found"}

        entity_id = row["entity_id"]
        now = datetime.now().isoformat()

        await self.db.execute(
            "UPDATE memory_questions SET status = 'answered', answer = ?, answered_at = ? WHERE id = ?",
            (answer, now, question_id),
        )

        # Write answer as a truth claim on the entity so reconciliation can use it
        claim = Claim(
            id=f"claim-answer-{uuid.uuid4().hex[:8]}",
            claim_type_key="truth",
            subject_id=entity_id,
            value=f"[Q: {row['question']}] {answer}",
            status="active",
            source_bulletins=[],
            created_at=datetime.now(),
        )
        await write_claim(self.db, claim)

        await self.db.execute(
            "UPDATE memory_questions SET answer_claim_id = ? WHERE id = ?",
            (claim.id, question_id),
        )

        # Queue entity for re-reconciliation
        self._schedule_reconciliation([entity_id])

        return {"status": "answered", "question_id": question_id, "claim_id": claim.id}

    async def dismiss_question(self, question_id: str) -> dict[str, Any]:
        """Dismiss a question without answering it."""
        row = await self.db.fetch_one(
            "SELECT id FROM memory_questions WHERE id = ? AND status = 'open'",
            (question_id,),
        )
        if not row:
            return {"status": "not_found"}

        await self.db.execute(
            "UPDATE memory_questions SET status = 'dismissed', answered_at = ? WHERE id = ?",
            (datetime.now().isoformat(), question_id),
        )
        return {"status": "dismissed", "question_id": question_id}

    # ── Silent-turn extraction ────────────────────────────────────

    async def _last_silent_turn_at(self, session_key: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT MAX(ran_at) AS a FROM memory_extraction_turns WHERE session_key = ?",
            (session_key,),
        )
        return row["a"] if row and row["a"] else None

    async def _has_undigested_messages(self, session_key: str) -> bool:
        """True if there are session messages newer than the last silent turn."""
        from bob_server.repositories.history import HistoryRepository
        history = HistoryRepository(self.db)
        active_from = await self._last_silent_turn_at(session_key)
        return bool(await history.count_dialogue(session_key, active_from))

    async def _render_silent_turn_history(
        self, session_key: str, *, max_history: int = 30, since_hours: float | None = None
    ) -> list[dict[str, Any]]:
        """Render recent session history as native role-structured messages.

        Assistant messages generated via memory recall are prefixed
        ``[SYNTHETIC]`` so the extractor can apply the corroboration rule;
        group user messages are prefixed ``[Name]`` for attribution. Tool-call
        replay is deliberately omitted — it is reply-turn noise for extraction.

        ``since_hours`` optionally restricts the window to messages newer than
        now - since_hours (used for one-off backfills like "process past 48h").
        ``max_history`` always caps the count as a safety bound.
        """
        is_group = ":group:" in session_key
        sender_names: dict[str, str] = {}
        if is_group:
            participants = await self.db.fetch_all(
                "SELECT contact_id, display_name FROM session_participants "
                "WHERE session_key = ?",
                (session_key,),
            )
            for p in participants:
                if p["contact_id"] and p["display_name"]:
                    sender_names[p["contact_id"]] = p["display_name"]

        from bob_server.repositories.history import HistoryRepository
        rows = await HistoryRepository(self.db).recent_dialogue(
            session_key, limit=max_history, since_hours=since_hours)

        messages: list[dict[str, Any]] = []
        for row in rows:
            content = (row["content"] or "").strip()
            if not content:
                continue
            if row["role"] == "assistant":
                if content.strip().upper().rstrip(".") in (
                    "NO_REPLY", "NO REPLY", "NOTHING TO SAY",
                ):
                    continue
                if row["synthetic"]:
                    content = f"[SYNTHETIC] {content}"
                messages.append({"role": "assistant", "content": content})
            else:
                if is_group and row["sender_id"]:
                    name = sender_names.get(row["sender_id"])
                    if name:
                        content = f"[{name}] {content}"
                messages.append({"role": "user", "content": content})
        return messages

    async def _build_silent_group_context(self, session_key: str) -> str:
        """Channel-type + participant roster block for the silent-turn prompt."""
        is_group = ":group:" in session_key
        if is_group:
            members = await self.db.fetch_all(
                "SELECT contact_id, display_name FROM session_participants "
                "WHERE session_key = ?",
                (session_key,),
            )
            roster = ", ".join(
                (m["display_name"] or m["contact_id"])
                for m in members
                if m["contact_id"]
            )
            line = "This conversation is a group chat."
            if roster:
                line += f" Participants: {roster}."
            line += (
                " Use list_entities / get_entity to find existing person-* and "
                "group-* entities before recording anything."
            )
            return f"# Channel context\n\n{line}"
        row = await self.db.fetch_one(
            "SELECT contact_id, display_name FROM session_participants "
            "WHERE session_key = ? LIMIT 1",
            (session_key,),
        )
        who = row["display_name"] if row and row["display_name"] else "the other participant"
        return (
            "# Channel context\n\n"
            f"This is a 1:1 conversation with {who}. "
            "Use list_entities / get_entity to find the existing person-* entity "
            "for them before recording anything."
        )

    @staticmethod
    def queue_remember_extraction(
        session_key: str, svc: "MemoryService", *, hint: str | None = None,
    ) -> None:
        """Queue a silent extraction turn to run once the current reply releases
        the session lock. Used by the ``remember`` tool.

        The task calls ``run_silent_turn_extraction``, which acquires the
        session's SessionDispatchGate internally; since the in-flight reply
        holds that lock, the task blocks there until the reply finishes and
        is stored, then proceeds. ``force=True`` honours Bob's explicit request
        even if the undigested-message guard would otherwise skip.
        """
        async def _deferred() -> None:
            try:
                await svc.run_silent_turn_extraction(
                    session_key, hint=hint, force=True, trigger="remember",
                )
            except Exception:
                logger.exception(
                    "Deferred remember extraction failed for %s", session_key,
                )

        task = asyncio.create_task(_deferred())
        _remember_tasks.add(task)
        task.add_done_callback(_remember_tasks.discard)

    async def run_silent_turn_extraction(
        self, session_key: str, *, max_history: int = 30, since_hours: float | None = None,
        hint: str | None = None, force: bool = False, trigger: str = "idle",
    ) -> dict[str, Any]:
        """Run an idle-triggered silent extraction turn over recent history.

        Drives an agent tool-loop on the memory model with a claim-creation
        tool subset. Every claim written is attributed to the synthetic
        assistant message this turn produces (``source_messages``). The turn
        is serialized with live reply turns via SessionDispatchGate.

        ``since_hours`` restricts the rendered window to messages newer than
        now - since_hours (for one-off backfills); defaults to the last
        ``max_history`` messages.

        ``hint`` adds a steering note to the instruction (e.g. a topic Bob
        flagged via the remember tool). ``force`` skips the undigested-message
        guards (for explicit remember-triggered turns). ``trigger`` labels the
        stored message metadata ("idle" vs "remember") for observability.
        """
        from bob_server.services.llm_dispatch import LLMDispatchService
        from bob_server.services.session_service import SessionService
        from bob_server.services.session_dispatch_gate import SessionDispatchGate
        from bob_server.services.memory.extraction_tools import make_extraction_tools
        from bob_server.services.memory.prompts import build_silent_turn_prompt
        from bob_server.services.memory.claim_types import build_extraction_prompt_section

        db = self.db
        settings = self.ctx.settings
        bot_name = getattr(settings.patience, "bot_name", None) or "Bob"

        # Quick pre-check before acquiring the lock.
        if not force and not await self._has_undigested_messages(session_key):
            return {"status": "skipped", "reason": "no_new_messages"}

        claim_types_section = build_extraction_prompt_section(list(ENTITY_TYPES))
        group_context = await self._build_silent_group_context(session_key)
        system_prompt = build_silent_turn_prompt(
            claim_types_section, bot_name=bot_name, group_context=group_context,
        )

        turn_message_id = f"msg-extr-{uuid.uuid4().hex[:12]}"
        dispatch_id = f"dispatch-silent-{uuid.uuid4().hex[:8]}"
        tools = make_extraction_tools(db, turn_message_id)
        # Capture the turn start as a comparison-friendly UTC timestamp so we
        # can find entities created during this turn (memory_entities.created_at
        # uses SQLite's datetime('now'), so we use the same format).
        turn_start_ts = utcnow().strftime("%Y-%m-%d %H:%M:%S")

        result_text = ""
        async with SessionDispatchGate.get_lock(session_key):
            # Re-check under the lock: another heartbeat may have run a turn
            # while we were waiting.
            if not force and not await self._has_undigested_messages(session_key):
                return {"status": "skipped", "reason": "race_handled"}

            history = await self._render_silent_turn_history(
                session_key, max_history=max_history, since_hours=since_hours
            )
            if not history:
                return {"status": "skipped", "reason": "empty_history"}

            # Final instruction triggers the tool loop. Without it the model sees
            # a conversation with no action to take and returns empty. An optional
            # hint (from the remember tool) steers attention without overriding
            # the quality rules.
            hint_block = ""
            if hint:
                hint_block = (
                    f'Bob flagged this conversation as worth reviewing now and '
                    f'pointed at: "{hint}". Give that particular attention — but '
                    f'still apply every quality rule; do not create a claim unless '
                    f'it genuinely holds up.\n\n'
                )
            instruction = (
                hint_block
                + "The messages above are the recent conversation in this channel, "
                "now idle. Review them and use the memory tools to record anything "
                "worth remembering about the people, groups, trips, or other "
                "entities involved — following the rules in the system prompt "
                "(only others' messages, never your own; weight replies to your "
                "[SYNTHETIC] lines as corroboration). Look up existing entities "
                "before writing to avoid duplicates. If genuinely nothing is worth "
                "remembering, reply with exactly: Nothing to record."
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt}, *history,
                {"role": "user", "content": instruction},
            ]
            llm = LLMDispatchService(self.ctx)
            result_text = await llm.chat_with_tools(
                messages, tools,
                model=llm.memory_model,
                max_iterations=25,
                call_category="memory_silent_turn",
                session_key=session_key,
                dispatch_id=dispatch_id,
            )

            # Claims were written via tool calls during the loop (before this
            # point), so count them now to store an accurate record.
            count_row = await db.fetch_one(
                "SELECT COUNT(*) AS n FROM memory_claims WHERE source_messages LIKE ?",
                (f'%"{turn_message_id}"%',),
            )
            claims_created = count_row["n"] if count_row else 0

            # Fetch the actual new-claim rows so we can surface them via the
            # verbose notice. Each row carries the typed value (or object_id
            # for entity-ref claims) so the user sees what was actually written.
            new_claim_rows = await db.fetch_all(
                "SELECT claim_type_key, subject_id, value, object_id "
                "FROM memory_claims "
                "WHERE status = 'active' AND source_messages LIKE ?",
                (f'%"{turn_message_id}"%',),
            ) if claims_created else []

            # New entities: subjects of new claims whose entity row was created
            # during this turn. Entities created via create_entity that did NOT
            # receive any claim in the same turn would be missed; in practice
            # the extractor always follows create_entity with add_claim calls,
            # so this captures them.
            new_entity_rows: list[dict[str, Any]] = []
            if new_claim_rows:
                subject_ids = list({r["subject_id"] for r in new_claim_rows})
                placeholders = ",".join("?" for _ in subject_ids)
                new_entity_rows = await db.fetch_all(
                    "SELECT entity_id, entity_type, display_name "
                    "FROM memory_entities "
                    f"WHERE entity_id IN ({placeholders}) "
                    "AND datetime(created_at) >= datetime(?) "
                    "AND status = 'active'",
                    (*subject_ids, turn_start_ts),
                )

            entities_created = len(new_entity_rows)

            # The model often ends with empty text after its tool calls; store a
            # meaningful summary rather than a misleading placeholder.
            if result_text and result_text.strip():
                content = result_text
            elif claims_created:
                content = f"[Silent extraction turn: recorded {claims_created} claim(s)]"
            else:
                content = "[Silent extraction turn: nothing memory-worthy]"

            await SessionService(self.ctx).add_message(
                session_key, "assistant", content,
                dispatch_id=dispatch_id,
                synthetic=True,
                provenance="extraction_marker",
                message_id=turn_message_id,
                metadata={"memory_extraction_turn": True, "trigger": trigger,
                          **({"hint": hint} if hint else {})},
            )

            # Verbose surface: if the session has memory_verbose enabled and
            # the turn actually recorded something, post a system-notice
            # message and publish an event for active transports to deliver.
            if entities_created or claims_created:
                await self._maybe_emit_verbose_notice(
                    session_key, turn_message_id, trigger,
                    new_entity_rows, new_claim_rows,
                )

        await db.execute(
            "INSERT INTO memory_extraction_turns "
            "(id, session_key, message_id, ran_at, claims_created) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                f"extr-{uuid.uuid4().hex[:10]}",
                session_key,
                turn_message_id,
                iso_utc(utcnow()),
                claims_created,
            ),
        )

        logger.info(
            "Silent turn %s: %d claim(s) recorded for session %s",
            turn_message_id, claims_created, session_key,
        )
        return {
            "status": "ok",
            "turn_message_id": turn_message_id,
            "claims_created": claims_created,
            "entities_created": entities_created,
        }

    async def _maybe_emit_verbose_notice(
        self,
        session_key: str,
        turn_message_id: str,
        trigger: str,
        new_entity_rows: list[dict[str, Any]],
        new_claim_rows: list[dict[str, Any]],
    ) -> None:
        """If the session has memory_verbose on, surface what the turn wrote.

        Posts a ``role='system'`` message in session_messages with metadata
        ``{"system_notice": True, "kind": "memory_verbose", ...}`` and publishes
        a ``memory.verbose_notice`` event on the event bus for active transports
        to deliver. Silently no-ops when the flag is off or there's nothing to
        report.
        """
        if not new_entity_rows and not new_claim_rows:
            return

        from bob_server.repositories.conversations import ConversationRepository
        policy = await ConversationRepository(self.db).get_policy(session_key)
        if not policy.get("memory_verbose"):
            return

        # Compose the human-readable notice.
        lines: list[str] = ["[memory] extraction turn"]
        if new_entity_rows:
            lines.append("New entities:")
            for e in new_entity_rows:
                label = e["display_name"] or e["entity_id"]
                lines.append(f"  - {e['entity_id']} ({e['entity_type']}) — {label}")
        if new_claim_rows:
            lines.append("New claims:")
            for c in new_claim_rows:
                if c["object_id"]:
                    val = f"→ {c['object_id']}"
                else:
                    val = f"= {c['value']}"
                lines.append(f"  - {c['subject_id']}.{c['claim_type_key']} {val}")
        notice = "\n".join(lines)

        from bob_server.services.session_service import SessionService
        await SessionService(self.ctx).add_message(
            session_key, "system", notice,
            metadata={
                "system_notice": True,
                "kind": "memory_verbose",
                "turn_message_id": turn_message_id,
                "trigger": trigger,
            },
        )
        if self.ctx.event_bus:
            await self.ctx.event_bus.publish(
                "memory.verbose_notice",
                {
                    "session_key": session_key,
                    "text": notice,
                    "turn_message_id": turn_message_id,
                    "trigger": trigger,
                },
            )

    # ── Entities ──────────────────────────────────────────────────

    async def ensure_self_entity(self) -> None:
        """Create the singleton self-bob entity if it does not exist.

        Called on service startup so that self-bob is always present as a
        write target for self-relevant claims. Idempotent via INSERT OR IGNORE.
        """
        now = utcnow()
        await self.db.execute(
            "INSERT OR IGNORE INTO memory_entities "
            "(entity_id, entity_type, display_name, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("self-bob", "self", "Bob", "active", now.isoformat()),
        )

    async def write_entity(self, workspace_dir: Path, entity: EntityDocument) -> str:
        """Write an entity record (identity only) to the database."""
        now = utcnow()
        status = entity.status if entity.status in ("active", "archived") else "active"

        await self.db.execute(
            "INSERT OR REPLACE INTO memory_entities "
            "(entity_id, entity_type, display_name, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                entity.entity_id,
                entity.entity_type,
                entity.display_name,
                status,
                now.isoformat(),
            ),
        )

        # Auto-create relationship-bob-{slug} when a person entity is written.
        # Gives extraction a stable target for relationship claims from day one.
        if entity.entity_type == "person" and entity.entity_id.startswith("person-"):
            person_slug = entity.entity_id.removeprefix("person-")
            relationship_id = f"relationship-bob-{person_slug}"
            await self.db.execute(
                "INSERT OR IGNORE INTO memory_entities "
                "(entity_id, entity_type, display_name, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    relationship_id,
                    "relationship",
                    f"Bob ↔ {entity.display_name}",
                    "active",
                    now.isoformat(),
                ),
            )
            participant_claim = Claim(
                id=f"claim-participant-{uuid.uuid4().hex[:8]}",
                claim_type_key="participant",
                subject_id=relationship_id,
                object_id=entity.entity_id,
            )
            try:
                await write_claim(self.db, participant_claim)
            except Exception:
                logger.exception("Failed to write participant claim for %s", relationship_id)

        # Update aliases
        await self.db.execute(
            "DELETE FROM memory_aliases WHERE entity_id = ?",
            (entity.entity_id,),
        )
        alias_params = []
        if entity.display_name:
            alias_params.append((entity.display_name, entity.entity_id))
            alias_params.append((entity.display_name.lower(), entity.entity_id))
        if alias_params:
            await self.db.execute_many(
                "INSERT OR IGNORE INTO memory_aliases (alias, entity_id) VALUES (?, ?)",
                alias_params,
            )

        # Update entity↔bulletin join rows
        if entity.source_bulletins:
            await self.db.execute_many(
                "INSERT OR IGNORE INTO memory_entity_bulletins (entity_id, bulletin_id) VALUES (?, ?)",
                [(entity.entity_id, bid) for bid in entity.source_bulletins],
            )

        # Render and update FTS
        await self._update_entity_fts(entity.entity_id)

        logger.info("Entity written: %s/%s", entity.entity_type, entity.entity_id)
        return entity.entity_id

    async def read_entity(self, workspace_dir: Path, entity_id: str) -> EntityDocument | None:
        """Read an entity record by ID."""
        row = await self.db.fetch_one(
            "SELECT * FROM memory_entities WHERE entity_id = ?",
            (entity_id,),
        )
        if not row:
            return None

        return EntityDocument(
            entity_id=row["entity_id"],
            entity_type=row["entity_type"],
            display_name=row["display_name"] or "",
            status=row["status"] or "active",
            source_bulletins=[],  # Not stored on entity in v7
        )

    async def list_entities(self, workspace_dir: Path, entity_type: str) -> list[EntityDocument]:
        """List all entities of a given type."""
        rows = await self.db.fetch_all(
            "SELECT * FROM memory_entities WHERE entity_type = ? ORDER BY entity_id",
            (entity_type,),
        )
        return [
            EntityDocument(
                entity_id=row["entity_id"],
                entity_type=entity_type,
                display_name=row["display_name"] or "",
                status=row["status"] or "active",
            )
            for row in rows
        ]

    async def _update_entity_fts(self, entity_id: str) -> None:
        """Render entity claims via template and update the FTS index."""
        from bob_server.services.memory.claim_service import update_entity_fts
        await update_entity_fts(self.db, entity_id)

    @staticmethod
    def _detect_entity_types_in_text(text: str) -> list[str]:
        """Detect likely entity types mentioned in text for claim type injection."""
        return detect_entity_types_in_text(text)

    async def _resolve_display_name(self, entity_id: str) -> str:
        """Try to resolve a display name for an entity ID."""
        etype = detect_entity_type(entity_id)
        et_def = ENTITY_TYPE_REGISTRY.get(etype)

        if et_def and et_def.display_name_claim:
            rows = await self.db.fetch_all(
                "SELECT value FROM memory_claims WHERE subject_id = ? "
                "AND claim_type_key = ? AND status = 'active' LIMIT 1",
                (entity_id, et_def.display_name_claim),
            )
            if rows and rows[0]["value"]:
                hex8 = rows[0]["value"][:8]
                from bob_server.repositories.contacts import ContactRepository
                row = await ContactRepository(self.db).get_by_id_prefix(hex8)
                if row and row["name"]:
                    return row["name"]
            slug = entity_id.removeprefix(et_def.prefix)
            return " ".join(part.capitalize() for part in slug.split("-"))

        return entity_id

    async def _list_all_entity_ids(self) -> set[str]:
        """List all entity IDs."""
        rows = await self.db.fetch_all("SELECT entity_id FROM memory_entities")
        return {r["entity_id"] for r in rows}

    # ── Retrieval ─────────────────────────────────────────────────

    async def search_entries(
        self, workspace_dir: Path, query: str, entity_type: str = ""
    ) -> dict[str, Any]:
        """Search memory using FTS5 across rendered entity bodies."""
        from bob_server.services.memory.tools import find

        if entity_type:
            results = await find(self.db, entity_type)
            return {"abstract": results, "results": []}

        # Build FTS query: AND individual tokens for broad matching
        tokens = query.strip().split()
        if not tokens:
            return {"abstract": "", "results": []}
        fts_parts = []
        for t in tokens:
            escaped = t.replace('"', '""')
            fts_parts.append(f'"{escaped}"')
        fts_query = " AND ".join(fts_parts)

        fts_rows = await self.db.fetch_all(
            "SELECT entity_id, display_name FROM memory_entities_fts "
            "WHERE memory_entities_fts MATCH ? LIMIT 20",
            (fts_query,),
        )

        # If FTS found nothing, try embedding search
        if not fts_rows:
            try:
                from bob_server.services.memory.embedding import search_similar
                emb_results = await search_similar(self.db, query, limit=10, threshold=1.2)
                if emb_results:
                    entity_ids = [r["entity_id"] for r in emb_results]
                    placeholders = ",".join("?" for _ in entity_ids)
                    emb_rows = await self.db.fetch_all(
                        f"SELECT e.entity_id, e.display_name, e.entity_type "
                        f"FROM memory_entities e WHERE e.entity_id IN ({placeholders}) AND e.status = 'active'",
                        tuple(entity_ids),
                    )
                    # Preserve distance ordering
                    row_map = {r["entity_id"]: r for r in emb_rows}
                    fts_rows = [row_map[eid] for eid in entity_ids if eid in row_map]
            except Exception:
                pass

        if not fts_rows:
            return {"abstract": f"No entities found matching: {query}", "results": []}

        results = [
            {
                "path": f"memory/{r['entity_id']}.md",
                "title": r["display_name"] or r["entity_id"],
                "relevance": "",
            }
            for r in fts_rows
        ]
        abstract = f"Found {len(results)} entities matching '{query}'"
        return {"abstract": abstract, "results": results}

    async def build_memory_index(self, workspace_dir: Path) -> str:
        """Build compact memory index for system prompt injection."""
        return await build_memory_index_text_db(self.db)

    async def merge_entities(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Detect and merge duplicate entities using embeddings + LLM."""
        from bob_server.services.memory.merge import run_merge
        from bob_server.services.llm_dispatch import LLMDispatchService

        llm = LLMDispatchService(self.ctx)
        return await run_merge(self.db, llm, dry_run=dry_run)

    async def rebuild_fts(self) -> int:
        """Rebuild the FTS5 index from scratch. Returns row count."""
        rows = await self.db.fetch_all("SELECT entity_id FROM memory_entities")
        for r in rows:
            await self._update_entity_fts(r["entity_id"])
        row = await self.db.fetch_one("SELECT count(*) AS c FROM memory_entities_fts")
        return row["c"] if row else 0

    async def rebuild_embeddings(self) -> int:
        """Rebuild embedding vectors for all entities. Returns count."""
        from bob_server.services.memory.embedding import embed_batch, upsert_embedding

        rows = await self.db.fetch_all(
            "SELECT entity_id, entity_type, display_name FROM memory_entities WHERE status = 'active'"
        )
        if not rows:
            return 0

        # Render all entities first
        rendered_map: dict[str, str] = {}
        for r in rows:
            claims = await self.db.fetch_all(
                "SELECT claim_type_key, object_id, value FROM memory_claims "
                "WHERE status = 'active' AND subject_id = ?",
                (r["entity_id"],),
            )
            claim_dicts = [
                {"claim_type_key": c["claim_type_key"], "object_id": c["object_id"], "value": c["value"]}
                for c in claims
            ]
            rendered_map[r["entity_id"]] = await render_entity(r["entity_type"], r["display_name"], claim_dicts, entity_id=r["entity_id"], db=self.db)

        # Batch embed (up to 100 at a time)
        entity_ids = list(rendered_map.keys())
        count = 0
        batch_size = 100
        for i in range(0, len(entity_ids), batch_size):
            batch_ids = entity_ids[i:i + batch_size]
            batch_texts = [rendered_map[eid] for eid in batch_ids]
            embeddings = await embed_batch(batch_texts)
            for eid, emb in zip(batch_ids, embeddings):
                if emb:
                    await upsert_embedding(self.db, eid, emb)
                    count += 1

        logger.info("Embedded %d entities", count)
        return count

    # ── Person/Contact helpers ────────────────────────────────────

    async def _get_contact_directory(self):
        """Load and cache ContactDirectory."""
        from bob_server.services.memory.contact_directory import ContactDirectory
        cache = getattr(self, "_contact_dir_cache", None)
        if cache is None and self.ctx and hasattr(self.ctx, "db") and self.ctx.db:
            cache = await ContactDirectory.load(self.ctx.db)
            self._contact_dir_cache = cache
        return cache

    @staticmethod
    def _format_contact_roster(directory: Any) -> str:
        """Format ContactDirectory as a person roster for the LLM prompt.

        Maps contact-{hex8} IDs to person-{slug} IDs so the LLM knows
        which person entity to use for each known contact.
        """
        if directory is None:
            return ""
        import re
        lines = []
        for record in directory._by_canonical.values():
            name = record.name
            slug = re.sub(r"[^a-z0-9\-]", "", name.strip().lower().replace(" ", "-"))
            person_id = f"person-{slug}"
            lines.append(f"- {record.canonical_id} ({name}) → {person_id}")
        return "\n".join(lines)

    @staticmethod
    def _build_contact_to_person_map(roster_text: str) -> dict[str, str]:
        """Parse the roster into a contact-{hex8} → person-{slug} map."""
        mapping: dict[str, str] = {}
        for line in roster_text.split("\n"):
            # Format: "- contact-{hex8} (Name) → person-{slug}"
            m = __import__("re").match(r"^- (contact-[a-f0-9]+) \((.+?)\) → (person-[\w-]+)$", line.strip())
            if m:
                mapping[m.group(1)] = m.group(3)
        return mapping

    @staticmethod
    def _premap_contact_tags(text: str, contact_map: dict[str, str]) -> str:
        """Replace {{contact:HEX8|Name}} tags with {{person-slug|Name}} in bulletin text."""
        import re
        def _replace(m: re.Match) -> str:
            hex8 = m.group(1)[:8]
            name = m.group(2)
            contact_id = f"contact-{hex8}"
            person_id = contact_map.get(contact_id, "")
            if person_id:
                return f"{{{{{person_id}|{name}}}}}"
            # Fallback: derive slug from name
            slug = re.sub(r"[^a-z0-9\-]", "", name.strip().lower().replace(" ", "-"))
            return f"{{{{person-{slug}|{name}}}}}"
        return re.sub(r"\{\{contact:([a-f0-9-]+)\|(.+?)\}\}", _replace, text)

    async def _load_group_members(self, source_id: str) -> list[str] | None:
        """Load group member canonical contact IDs for a session."""
        if not source_id:
            return None
        route = await self.db.fetch_one(
            "SELECT address, endpoint_kind FROM bindings WHERE session_key = ?",
            (source_id,),
        )
        if not route or route["endpoint_kind"] != "group" or not route["address"]:
            return None
        rows = await self.db.fetch_all(
            "SELECT gm.contact_id FROM whatsappgroup_members gm "
            "JOIN contacts c ON c.id = gm.contact_id "
            "WHERE gm.group_id = (SELECT id FROM whatsappgroups WHERE whatsapp_jid = ?) "
            "AND gm.left_at IS NULL",
            (route["address"],),
        )
        return [f"contact-{str(r['contact_id'])[:8]}" for r in rows]

    @staticmethod
    def _format_group_members(directory: Any, member_ids: list[str]) -> str:
        """Format group member list for the LLM prompt using person-{slug} IDs."""
        if not member_ids or directory is None:
            return ""
        import re
        parts = []
        for mid in member_ids:
            record = directory.get_by_canonical_id(mid)
            name = record.name if record else mid
            slug = re.sub(r"[^a-z0-9\-]", "", name.strip().lower().replace(" ", "-"))
            person_id = f"person-{slug}"
            parts.append(f"{person_id} ({name})")
        return ", ".join(parts)

    async def ensure_person_entry(
        self,
        workspace_dir: Path,
        *,
        contact_id: str,
        name: str,
        phone_number: str = "",
        email: str = "",
        channel: str = "",
    ) -> str | None:
        """Create a minimal person entity record if one doesn't exist.

        Lookup order: contact_id claim first (survives renames), then slug.
        Without the contact_id check, renaming a contact and then triggering
        this path (e.g. WhatsApp re-handshake) would create a duplicate
        person entity under the new slug with a second contact_id claim
        pointing at the same hex8.
        """
        if contact_id:
            existing_by_cid = await self.find_person_entry(
                workspace_dir, contact_id=contact_id,
            )
            if existing_by_cid:
                return existing_by_cid

        import re
        slug = re.sub(r"[^a-z0-9\-]", "", name.strip().lower().replace(" ", "-"))
        person_id = f"person-{slug}"

        existing = await self.read_entity(workspace_dir, person_id)
        if existing:
            return person_id

        entity = EntityDocument(
            entity_id=person_id,
            entity_type="person",
            display_name=name,
            status="active",
        )
        await self.write_entity(workspace_dir, entity)

        # Write a contact_id claim linking person to contacts table row
        hex8 = contact_id[:8]
        from bob_server.services.memory.claim_service import write_claim
        claim = Claim(
            id=f"claim-person-{person_id}-contact_id",
            claim_type_key="contact_id",
            subject_id=person_id,
            value=hex8,
            status="active",
            visibility="private",
        )
        await write_claim(self.db, claim)

        return person_id

    async def find_person_entry(
        self,
        workspace_dir: Path,
        *,
        contact_id: str = "",
        name: str = "",
    ) -> str | None:
        """Find a person entity by name (slug) or by contact_id claim."""
        if name:
            import re
            slug = re.sub(r"[^a-z0-9\-]", "", name.strip().lower().replace(" ", "-"))
            person_id = f"person-{slug}"
            entity = await self.read_entity(workspace_dir, person_id)
            if entity:
                return entity.entity_id
        if contact_id:
            hex8 = contact_id[:8]
            rows = await self.db.fetch_all(
                "SELECT subject_id FROM memory_claims "
                "WHERE claim_type_key = 'contact_id' AND value = ? AND status = 'active' LIMIT 1",
                (hex8,),
            )
            if rows:
                return rows[0]["subject_id"]
        return None

    async def sync_person_display_name_for_contact(
        self, contact_id: str, new_name: str
    ) -> str | None:
        """Update display_name on person entities linked to this contact.

        Called whenever a contact is renamed so the linked entity's frozen
        display_name snapshot stays in sync. Refreshes FTS + embedding via
        _update_entity_fts.
        """
        if not contact_id or not new_name:
            return None
        hex8 = contact_id[:8]
        rows = await self.db.fetch_all(
            "SELECT subject_id FROM memory_claims "
            "WHERE claim_type_key = 'contact_id' AND value = ? AND status = 'active'",
            (hex8,),
        )
        if not rows:
            return None
        if len(rows) > 1:
            logger.warning(
                "multiple entities linked to contact %s: %s",
                contact_id, [r["subject_id"] for r in rows],
            )
        for row in rows:
            eid = row["subject_id"]
            await self.db.execute(
                "UPDATE memory_entities SET display_name = ? "
                "WHERE entity_id = ? AND status = 'active'",
                (new_name, eid),
            )
            await self._update_entity_fts(eid)
        return rows[0]["subject_id"]

    async def retire_contact_id_claim(self, contact_id: str) -> int:
        """Mark active contact_id claims for this contact as superseded.

        Called when a contact is soft-deleted so the link doesn't dangle
        and resolve to a missing row. Mirrors reconciliation.py's pattern
        of retiring claims without writing a replacement.
        """
        if not contact_id:
            return 0
        hex8 = contact_id[:8]
        return await self.db.execute(
            "UPDATE memory_claims SET status = 'superseded' "
            "WHERE claim_type_key = 'contact_id' AND value = ? AND status = 'active'",
            (hex8,),
        )

    # ── Group helpers ─────────────────────────────────────────────

    async def _resolve_group_entity_id(self, source_id: str) -> str | None:
        """Look up the group entity ID for a bulletin's source session."""
        if not source_id:
            return None
        route = await self.db.fetch_one(
            "SELECT address, endpoint_kind FROM bindings WHERE session_key = ?",
            (source_id,),
        )
        if not route or route["endpoint_kind"] != "group" or not route["address"]:
            return None
        row = await self.db.fetch_one(
            "SELECT memory_entity_id FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (route["address"],),
        )
        return row["memory_entity_id"] if row and row["memory_entity_id"] else None

    async def ensure_group_entity(
        self,
        workspace_dir: Path,
        session_key: str,
        bulletin_id: str,
    ) -> str | None:
        """Ensure a group entity exists for a group session and link the bulletin."""
        route = await self.db.fetch_one(
            "SELECT address, endpoint_kind FROM bindings WHERE session_key = ?",
            (session_key,),
        )
        if not route or route["endpoint_kind"] != "group" or not route["address"]:
            return None

        chat_id = route["address"]

        group_row = await self.db.fetch_one(
            "SELECT id, name, description, memory_entity_id, member_count "
            "FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (chat_id,),
        )
        if not group_row:
            return None

        group_name = group_row["name"] or chat_id
        existing_entity_id = group_row["memory_entity_id"]

        if existing_entity_id:
            entity = await self.read_entity(workspace_dir, existing_entity_id)
            if entity and entity.display_name != group_name:
                entity.display_name = group_name
                await self.write_entity(workspace_dir, entity)
        else:
            entity_id = f"group-{uuid.uuid4().hex[:8]}"
            entity = EntityDocument(
                entity_id=entity_id,
                entity_type="group",
                display_name=group_name,
                status="active",
            )
            await self.write_entity(workspace_dir, entity)
            existing_entity_id = entity_id

            await self.db.execute(
                "UPDATE whatsappgroups SET memory_entity_id = ? WHERE id = ?",
                (entity_id, group_row["id"]),
            )

        await self.db.execute(
            "INSERT OR IGNORE INTO memory_entity_bulletins (entity_id, bulletin_id) VALUES (?, ?)",
            (existing_entity_id, bulletin_id),
        )

        return existing_entity_id

    # ── Validation ────────────────────────────────────────────────

    async def validate(self, workspace_dir: Path) -> dict[str, Any]:
        """Validate memory data."""
        issues: list[str] = []
        rows = await self.db.fetch_all(
            "SELECT entity_id FROM memory_entities WHERE display_name = '' OR entity_type = ''"
        )
        for r in rows:
            issues.append(f"{r['entity_id']}: missing display_name or entity_type")
        return {"valid": len(issues) == 0, "issues": issues}

    # ── Legacy compatibility ──────────────────────────────────────

    async def browse_category(self, workspace_dir: Path, wiki: str, category: str) -> list[dict[str, Any]]:
        """Legacy: browse entities by type."""
        return [
            {"slug": e.entity_id, "title": e.display_name, "modified": 0}
            for e in await self.list_entities(workspace_dir, category)
        ]

    async def read_entry(self, workspace_dir: Path, wiki: str, category: str, slug: str) -> str | None:
        """Legacy: read an entity by slug."""
        entity = await self.read_entity(workspace_dir, slug)
        if entity:
            return entity.display_name
        return None

    async def write_entry(self, workspace_dir: Path, wiki: str, category: str, slug: str, title: str, content: str) -> str:
        """Legacy: write an entity."""
        entity = EntityDocument(
            entity_id=slug,
            entity_type=category,
            display_name=title,
        )
        return await self.write_entity(workspace_dir, entity)

    async def list_recent_entries(self, workspace_dir: Path, wiki_names: list[str], limit: int = 50) -> dict[str, Any]:
        """Legacy: list recent entity documents."""
        rows = await self.db.fetch_all(
            "SELECT entity_id, entity_type, display_name, updated_at "
            "FROM memory_entities ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        entries = [
            {
                "path": r["entity_id"],
                "wiki": "core",
                "category": r["entity_type"],
                "slug": r["entity_id"],
                "title": r["display_name"] or "",
                "summary": "",
                "modified": r["updated_at"],
            }
            for r in rows
        ]
        return {
            "stats": {"total_entries": len(rows)},
            "recent": entries,
        }

    async def resolve_accessible_wikis(self, workspace_dir: Path, session_key: str | None = None) -> list[str]:
        return ["core"]

    async def resolve_writable_wikis(self, workspace_dir: Path, session_key: str | None = None) -> list[str]:
        return ["core"]

    def validate_wiki_category(self, workspace_dir: Path, wiki: str, category: str) -> bool:
        return True


async def build_memory_index_text_db(db: Any) -> str:
    """Build a compact memory index from claims for system prompt injection."""
    rows = await db.fetch_all(
        "SELECT entity_type, entity_id, display_name "
        "FROM memory_entities ORDER BY entity_type, entity_id"
    )
    if not rows:
        return ""

    by_type: dict[str, list[str]] = {}
    for r in rows:
        entry_str = r["display_name"] or r["entity_id"]
        by_type.setdefault(r["entity_type"], []).append(entry_str)

    lines = [f"**{t}**: " + ", ".join(entries) for t, entries in sorted(by_type.items())]
    return "\n".join(lines)
