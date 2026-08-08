"""Memory tools for LLM function calling (v7 claim-centric).

Usage:
    tools.extend(make_memory_tools(ctx, session_key=session_key))
"""

from __future__ import annotations

import json
import logging

from bob_server.context import AppContext
from bob_server.services.memory import MemoryService
from bob_server.services.memory.channels import resolve_channel_id
from bob_server.services.memory.models import ENTITY_TYPES
from bob_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)


def make_memory_tools(ctx: AppContext, *, session_key: str) -> list[Tool]:
    """Create memory recall/find/note tools bound to the given context."""

    svc = MemoryService(ctx)

    @tool
    async def recall(query: str) -> str:
        """Retrieve entity information by ID, name, or natural language query.
        Returns the entity's claims rendered as readable text."""
        from bob_server.services.memory.tools import recall as _recall
        return await _recall(ctx.db, query)

    async def _find_handler(
        entity_type: str,
        claim_type_key: str = "",
        value: str = "",
    ) -> str:
        from bob_server.services.memory.tools import find as _find
        return await _find(ctx.db, entity_type, claim_type_key or None, value or None)

    find = Tool(
        name="find",
        description=(
            f"Find entities by type with optional claim filters. "
            f"Entity types: {', '.join(ENTITY_TYPES)}. "
            f"Use this to list dayplans for a trip, find a dayplan by date "
            f"(find(\"dayplan\", \"date\", \"2026-06-30\")), list daylogs, "
            f"find attractions at a location, etc. "
            f"Returns matching entity IDs and display names."
        ),
        parameters={
            "entity_type": {"type": "string"},
            "claim_type_key": {"type": "string"},
            "value": {"type": "string"},
        },
        required=["entity_type"],
        handler=_find_handler,
    )

    @tool
    async def note(
        text: str,
        context_entity_id: str = "",
    ) -> str:
        """Accept new information from conversation. Queues as a bulletin for digestion.
        Optionally link to a context entity ID (e.g. trip-bali-2026)."""
        from bob_server.services.memory.tools import note as _note
        channel_id = resolve_channel_id(session_key)
        return await _note(ctx.db, text, context_entity_id or None, channel_id=channel_id)

    @tool
    async def remember(hint: str = "") -> str:
        """Flag the current conversation as worth capturing now. Queues a memory
        extraction turn that runs immediately after this reply completes (silent
        extraction mode only). Optional `hint` steers the extractor toward a topic
        (e.g. "user updated their email"). Use sparingly — only when something
        genuinely memory-worthy just happened; idle conversations are already
        mined automatically once they go quiet."""
        MemoryService.queue_remember_extraction(session_key, svc, hint=hint or None)
        return json.dumps({"ok": True, "queued": True, "hint": bool(hint)})

    @tool
    async def memory_write(
        content: str,
        channel_id: str = "",
        visibility: str = "private",
    ) -> str:
        """Create a memory bulletin. Content is markdown.
        Queued for digestion into claims. Use note() for simpler input."""
        workspace = ctx.settings.harness.workspace_dir

        cid = channel_id or resolve_channel_id(session_key)

        bulletin_id = await svc.write_bulletin(
            workspace,
            channel_id=cid,
            source_type="manual",
            source_id=session_key,
            content=content,
            visibility=visibility,
        )
        return json.dumps({"ok": True, "bulletin_id": bulletin_id, "queued": True})

    @tool
    async def memory_correct(
        action: str,
        entity_id: str = "",
        claim_type_key: str = "",
        value: str = "",
        reason: str = "",
        new_entity_id: str = "",
        new_display_name: str = "",
        new_value: str = "",
        new_object_id: str = "",
        entity_type: str = "",
        claims_json: str = "[]",
    ) -> str:
        """Correct or remove wrong memory data. Actions:
        - "remove_entity": Archive an entity and supersede all its claims. Use for hallucinated/incorrect entities.
        - "remove_claim": Supersede a specific claim on an entity. Requires entity_id, claim_type_key, and value.
        - "add_claim": Add a single new claim to an existing entity. Requires entity_id, claim_type_key, and exactly one of new_value (scalar) or new_object_id (relation). Use this when you want to attach a note/date/etc. without disturbing other claims of the same type. For user-stated corrections ('actually...', 'that's wrong'), use set_truth instead.
        - "replace_claim": Supersede matching claims and write a corrected one in one shot. Requires entity_id, claim_type_key, and exactly one of new_value (for scalar claims like date/notes) or new_object_id (for relation claims like associated_trip). Optional value filters which claims to supersede.
        - "set_truth": Write a truth claim on an entity (user-stated correction that overrides inference). Subject entity must already exist.
        - "rename_entity": Change an entity's ID (e.g. daylog-bali-aug3 -> daylog-bali-aug4 when the date was wrong). Requires entity_id, new_entity_id (must match the entity's type prefix). Rewrites all claim/bulletin/relation refs. Optional new_display_name updates the label too.
        - "create_entity": Create a new typed entity (e.g. a missing daylog) and optionally attach initial claims. Requires entity_id (must match entity_type prefix), entity_type. Optional claims_json: JSON array of [{"claim_type_key": "...", "value": "..."} or {"claim_type_key": "...", "object_id": "..."}]. Optional new_display_name overrides the default label. Use this when you need to materialize an entity that should exist but doesn't (e.g. relocating a misplaced note to its own daylog).
        Always provide a reason explaining why the correction is needed."""
        from bob_server.services.memory.claim_service import write_claim, update_entity_fts
        from bob_server.services.memory.models import Claim
        from datetime import datetime
        import uuid

        if not reason:
            return json.dumps({"error": "reason is required for all corrections"})

        if action == "remove_entity":
            if not entity_id:
                return json.dumps({"error": "entity_id is required for remove_entity"})
            # Check entity exists
            row = await ctx.db.fetch_one(
                "SELECT entity_id, entity_type FROM memory_entities WHERE entity_id = ? AND status = 'active'",
                (entity_id,),
            )
            if not row:
                return json.dumps({"error": f"Entity not found or already archived: {entity_id}"})

            # Archive the entity
            await ctx.db.execute(
                "UPDATE memory_entities SET status = 'archived' WHERE entity_id = ?",
                (entity_id,),
            )
            # Supersede all active claims
            claims = await ctx.db.fetch_all(
                "SELECT id FROM memory_claims WHERE subject_id = ? AND status = 'active'",
                (entity_id,),
            )
            for c in claims:
                await ctx.db.execute(
                    "UPDATE memory_claims SET status = 'superseded' WHERE id = ?",
                    (c["id"],),
                )
            # Also remove claims referencing this entity as object_id
            ref_claims = await ctx.db.fetch_all(
                "SELECT id FROM memory_claims WHERE object_id = ? AND status = 'active'",
                (entity_id,),
            )
            for c in ref_claims:
                await ctx.db.execute(
                    "UPDATE memory_claims SET status = 'superseded' WHERE id = ?",
                    (c["id"],),
                )
            # Write a truth claim to prevent re-creation
            truth_claim = Claim(
                id=f"claim-correct-{uuid.uuid4().hex[:8]}",
                claim_type_key="truth",
                subject_id=entity_id,
                value=f"[removed] {reason}",
                status="active",
                source_bulletins=[],
                created_at=datetime.now(),
            )
            await write_claim(ctx.db, truth_claim)
            await update_entity_fts(ctx.db, entity_id)

            logger.info("Entity removed via memory_correct: %s (%d claims, %d refs) — %s",
                       entity_id, len(claims), len(ref_claims), reason)
            return json.dumps({
                "ok": True,
                "action": "remove_entity",
                "entity_id": entity_id,
                "claims_archived": len(claims),
                "references_removed": len(ref_claims),
            })

        elif action == "remove_claim":
            if not entity_id or not claim_type_key:
                return json.dumps({"error": "entity_id and claim_type_key required for remove_claim"})
            # Find matching active claims
            params: list = [entity_id, claim_type_key]
            extra = ""
            if value:
                extra = " AND (value = ? OR object_id = ?)"
                params.extend([value, value])
            rows = await ctx.db.fetch_all(
                f"SELECT id FROM memory_claims WHERE subject_id = ? AND claim_type_key = ? AND status = 'active'{extra}",
                tuple(params),
            )
            if not rows:
                return json.dumps({"error": f"No matching active claim found"})
            for r in rows:
                await ctx.db.execute(
                    "UPDATE memory_claims SET status = 'superseded' WHERE id = ?",
                    (r["id"],),
                )
            # Write truth claim
            truth_claim = Claim(
                id=f"claim-correct-{uuid.uuid4().hex[:8]}",
                claim_type_key="truth",
                subject_id=entity_id,
                value=f"[removed {claim_type_key}] {reason}",
                status="active",
                source_bulletins=[],
                created_at=datetime.now(),
            )
            await write_claim(ctx.db, truth_claim)
            await update_entity_fts(ctx.db, entity_id)
            return json.dumps({
                "ok": True,
                "action": "remove_claim",
                "entity_id": entity_id,
                "claims_removed": len(rows),
            })

        elif action == "add_claim":
            if not entity_id or not claim_type_key:
                return json.dumps({"error": "entity_id and claim_type_key required for add_claim"})
            if not new_value and not new_object_id:
                return json.dumps({"error": "new_value or new_object_id required for add_claim"})
            if new_value and new_object_id:
                return json.dumps({"error": "Provide exactly one of new_value or new_object_id, not both"})
            subject_row = await ctx.db.fetch_one(
                "SELECT 1 FROM memory_entities WHERE entity_id = ?",
                (entity_id,),
            )
            if not subject_row:
                return json.dumps({
                    "error": f"subject_id {entity_id!r} has no row in memory_entities — "
                             f"add_claim cannot create entities. Use action=create_entity first."
                })
            claim = Claim(
                id=f"claim-correct-{uuid.uuid4().hex[:8]}",
                claim_type_key=claim_type_key,
                subject_id=entity_id,
                object_id=new_object_id or None,
                value=new_value or None,
                status="active",
                source_bulletins=[],
                created_at=datetime.now(),
            )
            await write_claim(ctx.db, claim)
            await update_entity_fts(ctx.db, entity_id)
            logger.info("Claim added via memory_correct: %s [%s] on %s — %s",
                       claim_type_key, new_value or new_object_id, entity_id, reason)
            return json.dumps({
                "ok": True,
                "action": "add_claim",
                "entity_id": entity_id,
                "claim_type_key": claim_type_key,
                "claim_id": claim.id,
            })

        elif action == "set_truth":
            if not entity_id or not value:
                return json.dumps({"error": "entity_id and value required for set_truth"})
            subject_row = await ctx.db.fetch_one(
                "SELECT 1 FROM memory_entities WHERE entity_id = ?",
                (entity_id,),
            )
            if not subject_row:
                return json.dumps({
                    "error": f"subject_id {entity_id!r} has no row in memory_entities — "
                             f"set_truth cannot create entities. Use action=create_entity first."
                })
            claim = Claim(
                id=f"claim-correct-{uuid.uuid4().hex[:8]}",
                claim_type_key="truth",
                subject_id=entity_id,
                value=value,
                status="active",
                source_bulletins=[],
                created_at=datetime.now(),
            )
            await write_claim(ctx.db, claim)
            await update_entity_fts(ctx.db, entity_id)
            return json.dumps({
                "ok": True,
                "action": "set_truth",
                "entity_id": entity_id,
                "claim_id": claim.id,
            })

        elif action == "rename_entity":
            if not entity_id or not new_entity_id:
                return json.dumps({"error": "entity_id and new_entity_id required for rename_entity"})
            row = await ctx.db.fetch_one(
                "SELECT entity_id, entity_type, display_name FROM memory_entities WHERE entity_id = ? AND status = 'active'",
                (entity_id,),
            )
            if not row:
                return json.dumps({"error": f"Entity not found or already archived: {entity_id}"})
            existing = await ctx.db.fetch_one(
                "SELECT 1 FROM memory_entities WHERE entity_id = ?",
                (new_entity_id,),
            )
            if existing:
                return json.dumps({"error": f"Target entity_id already exists: {new_entity_id}"})
            expected_prefix = f"{row['entity_type']}-"
            if not new_entity_id.startswith(expected_prefix):
                return json.dumps({
                    "error": f"new_entity_id must start with {expected_prefix!r} (entity type is {row['entity_type']!r})"
                })
            if new_display_name:
                await ctx.db.execute(
                    "UPDATE memory_entities SET entity_id = ?, display_name = ? WHERE entity_id = ?",
                    (new_entity_id, new_display_name, entity_id),
                )
            else:
                await ctx.db.execute(
                    "UPDATE memory_entities SET entity_id = ? WHERE entity_id = ?",
                    (new_entity_id, entity_id),
                )
            from bob_server.services.memory.cleanup import (
                rewrite_claims, rewrite_bulletin_entities, rewrite_entity_relations,
            )
            rename_map = {entity_id: new_entity_id}
            rewritten_claims = await rewrite_claims(ctx.db, rename_map)
            rewritten_bulletins = await rewrite_bulletin_entities(ctx.db, rename_map)
            rewritten_related = await rewrite_entity_relations(ctx.db, rename_map)
            # Old FTS row and embedding reference an entity_id that no longer exists
            await ctx.db.execute(
                "DELETE FROM memory_entities_fts WHERE entity_id = ?", (entity_id,)
            )
            await ctx.db.execute(
                "DELETE FROM memory_entity_embeddings WHERE entity_id = ?", (entity_id,)
            )
            await update_entity_fts(ctx.db, new_entity_id)

            logger.info("Entity renamed via memory_correct: %s -> %s — %s",
                       entity_id, new_entity_id, reason)
            return json.dumps({
                "ok": True,
                "action": "rename_entity",
                "old_entity_id": entity_id,
                "new_entity_id": new_entity_id,
                "rewritten_claims": rewritten_claims,
                "rewritten_bulletins": rewritten_bulletins,
                "rewritten_related": rewritten_related,
            })

        elif action == "replace_claim":
            if not entity_id or not claim_type_key:
                return json.dumps({"error": "entity_id and claim_type_key required for replace_claim"})
            if not new_value and not new_object_id:
                return json.dumps({"error": "new_value or new_object_id required for replace_claim"})
            if new_value and new_object_id:
                return json.dumps({"error": "Provide exactly one of new_value or new_object_id, not both"})
            subject_row = await ctx.db.fetch_one(
                "SELECT 1 FROM memory_entities WHERE entity_id = ?",
                (entity_id,),
            )
            if not subject_row:
                return json.dumps({
                    "error": f"subject_id {entity_id!r} has no row in memory_entities — "
                             f"replace_claim cannot create entities. Use action=create_entity first."
                })
            params: list = [entity_id, claim_type_key]
            extra = ""
            if value:
                extra = " AND (value = ? OR object_id = ?)"
                params.extend([value, value])
            rows = await ctx.db.fetch_all(
                f"SELECT id FROM memory_claims WHERE subject_id = ? AND claim_type_key = ? AND status = 'active'{extra}",
                tuple(params),
            )
            for r in rows:
                await ctx.db.execute(
                    "UPDATE memory_claims SET status = 'superseded' WHERE id = ?",
                    (r["id"],),
                )
            replacement = Claim(
                id=f"claim-correct-{uuid.uuid4().hex[:8]}",
                claim_type_key=claim_type_key,
                subject_id=entity_id,
                object_id=new_object_id or None,
                value=new_value or None,
                status="active",
                source_bulletins=[],
                created_at=datetime.now(),
            )
            await write_claim(ctx.db, replacement)
            await update_entity_fts(ctx.db, entity_id)
            logger.info("Claim replaced via memory_correct: %s [%s] on %s — %s",
                       claim_type_key, value or "(any)", entity_id, reason)
            return json.dumps({
                "ok": True,
                "action": "replace_claim",
                "entity_id": entity_id,
                "claim_type_key": claim_type_key,
                "superseded": len(rows),
                "new_claim_id": replacement.id,
            })

        elif action == "create_entity":
            if not entity_id or not entity_type:
                return json.dumps({"error": "entity_id and entity_type required for create_entity"})
            if entity_type not in ENTITY_TYPES:
                return json.dumps({
                    "error": f"Unknown entity_type {entity_type!r}. Known types: {', '.join(ENTITY_TYPES)}"
                })
            expected_prefix = f"{entity_type}-"
            if not entity_id.startswith(expected_prefix):
                return json.dumps({
                    "error": f"entity_id must start with {expected_prefix!r} to match entity_type {entity_type!r}"
                })
            existing = await ctx.db.fetch_one(
                "SELECT 1 FROM memory_entities WHERE entity_id = ?",
                (entity_id,),
            )
            if existing:
                return json.dumps({
                    "error": f"Entity already exists: {entity_id}. Use replace_claim/set_truth on it instead."
                })
            display_name = new_display_name or (
                entity_id.split("-", 1)[-1].replace("-", " ").title() if "-" in entity_id else entity_id
            )
            await ctx.db.execute(
                "INSERT OR IGNORE INTO memory_entities (entity_id, entity_type, display_name, status) "
                "VALUES (?, ?, ?, 'active')",
                (entity_id, entity_type, display_name),
            )
            try:
                new_claims = json.loads(claims_json) if claims_json else []
            except json.JSONDecodeError:
                return json.dumps({
                    "error": f"Created entity {entity_id} but claims_json was invalid JSON.",
                    "entity_id": entity_id,
                })
            if not isinstance(new_claims, list):
                return json.dumps({
                    "error": "claims_json must be a JSON array of claim objects.",
                    "entity_id": entity_id,
                })
            written: list[str] = []
            skipped: list[str] = []
            for cl in new_claims:
                if not isinstance(cl, dict):
                    continue
                ctk = cl.get("claim_type_key", "")
                if not ctk:
                    skipped.append("(missing claim_type_key)")
                    continue
                cl_value = cl.get("value") or None
                cl_object = cl.get("object_id") or None
                if cl_value and cl_object:
                    cl_object = None
                if not cl_value and not cl_object:
                    skipped.append(f"{ctk}: no value or object_id")
                    continue
                claim = Claim(
                    id=f"claim-correct-{uuid.uuid4().hex[:8]}",
                    claim_type_key=ctk,
                    subject_id=entity_id,
                    object_id=cl_object,
                    value=cl_value,
                    status="active",
                    source_bulletins=[],
                    created_at=datetime.now(),
                )
                await write_claim(ctx.db, claim)
                written.append(ctk)
            await update_entity_fts(ctx.db, entity_id)
            logger.info("Entity created via memory_correct: %s (%s) with %d claims — %s",
                       entity_id, entity_type, len(written), reason)
            return json.dumps({
                "ok": True,
                "action": "create_entity",
                "entity_id": entity_id,
                "entity_type": entity_type,
                "display_name": display_name,
                "claims_written": written,
                "claims_skipped": skipped,
            })

        else:
            return json.dumps({"error": f"Unknown action: {action}. Use remove_entity, remove_claim, add_claim, replace_claim, set_truth, rename_entity, or create_entity."})

    # In silent extraction mode the bulletin-writing tools (note, memory_write)
    # are superseded by the remember tool — Bob flags the conversation for the
    # extractor instead of authoring a bulletin that the dream pipeline digests.
    # In bulletin mode the legacy note/memory_write tools are offered.
    mode = ctx.settings.memory_extraction.mode
    capture_tools = [remember] if mode == "silent" else [note, memory_write]
    return [recall, find, *capture_tools, memory_correct]
