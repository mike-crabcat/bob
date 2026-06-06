"""MemoryService v6 — SQLite-backed channel-centric memory system."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from cyborg_server.services.base import BaseService, utcnow
from cyborg_server.services.memory.models import (
    ENTITY_CATEGORIES,
    Bulletin,
    Claim,
    EntityDocument,
    parse_frontmatter,
    serialize_frontmatter,
)
from cyborg_server.services.memory.claim_service import (
    extract_claims_from_bulletin,
    write_claim,
)
from cyborg_server.services.memory.entity_resolver import (
    canonical_contact_id,
    normalize_entity_id,
)
from cyborg_server.services.memory.prompts import ENTITY_PATCH_PROMPT, ENTITY_UPDATE_PROMPT

logger = logging.getLogger(__name__)

_RELATED_ENTITIES_RE = re.compile(
    r"## Related Entities\n+(.*?)(?=\n## |\Z)", re.DOTALL
)


_ENTITY_TYPE_PREFIXES: dict[str, str] = {
    "contact-": "contact", "group-": "group", "channel-": "channel",
    "trip-": "trip", "location-": "location", "event-": "event",
    "task-": "task", "artifact-": "artifact", "decision-": "decision",
}


def _detect_entity_type(entity_id: str) -> str:
    for prefix, etype in _ENTITY_TYPE_PREFIXES.items():
        if entity_id.startswith(prefix):
            return etype
    return "channel"


_DISPLAY_NAME_RE = re.compile(r"^display_name:\\s*(.+)$", re.MULTILINE)


def _extract_display_name(body: str) -> str:
    m = _DISPLAY_NAME_RE.search(body)
    return m.group(1).strip() if m else ""


def _parse_related_from_body(body: str) -> dict[str, list[str]]:
    """Extract related entity IDs from the Related Entities section in body text."""
    m = _RELATED_ENTITIES_RE.search(body)
    if not m:
        return {}
    section = m.group(1)
    result: dict[str, list[str]] = {}
    for line in section.splitlines():
        line = line.strip()
        if not line or line.endswith(": []") or line.endswith(":"):
            continue
        cat, _, rest = line.partition(":")
        cat = cat.strip()
        if not rest.strip():
            continue
        # Parse bracket list: [id1, id2] or bare id
        val = rest.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            if inner:
                ids = [x.strip().strip("\"'") for x in inner.split(",") if x.strip()]
            else:
                ids = []
        else:
            ids = [val]
        if ids:
            result[cat] = ids
    return result


class MemoryService(BaseService):
    """Reads and writes v6 memory via SQLite: bulletins, claims, entities."""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)

    @staticmethod
    def _memory_dir(workspace_dir: Path) -> Path:
        return workspace_dir.expanduser() / "memory"

    # ── Setup ─────────────────────────────────────────────────────

    @staticmethod
    def ensure_memory_structure(workspace_dir: Path) -> None:
        """No-op — tables are created by schema migrations."""

    # ── Bulletins ─────────────────────────────────────────────────

    async def write_bulletin(
        self,
        workspace_dir: Path,
        *,
        channel_id: str,
        source_type: str,
        source_id: str = "",
        content: str,
        visibility: str = "private",
        occurred_at: str | None = None,
        session_range_start: str = "",
        session_range_end: str = "",
    ) -> str:
        """Write an immutable plain-text bulletin to the database."""
        now = utcnow()
        date_str = now.strftime("%Y-%m-%d")
        bulletin_id = f"bulletin-{date_str}-{uuid.uuid4().hex[:6]}"
        ts = occurred_at or now.isoformat()

        await self.db.execute(
            "INSERT INTO memory_bulletins "
            "(id, created_at, channel_id, source_type, source_id, visibility, content, "
            " session_range_start, session_range_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bulletin_id,
                ts,
                channel_id,
                source_type,
                source_id,
                visibility,
                content,
                session_range_start,
                session_range_end,
            ),
        )

        logger.info("Bulletin written: %s", bulletin_id)
        return bulletin_id

    async def read_bulletins(
        self,
        workspace_dir: Path,
        *,
        limit: int = 50,
        from_date: str | None = None,
        skip_digested: bool = False,
        oldest_first: bool = False,
    ) -> list[Bulletin]:
        """Read bulletins from the database."""
        query = "SELECT * FROM memory_bulletins"
        conditions: list[str] = []
        params: list[Any] = []

        if skip_digested:
            conditions.append("digested = 0")
        if from_date:
            conditions.append("created_at >= ?")
            params.append(from_date)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += f" ORDER BY created_at {'ASC' if oldest_first else 'DESC'} LIMIT ?"
        params.append(limit)

        rows = await self.db.fetch_all(query, tuple(params))
        if not rows:
            return []

        results: list[Bulletin] = []
        for row in rows:
            results.append(Bulletin(
                id=row["id"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
                channel_id=row["channel_id"] or "",
                source_type=row["source_type"] or "",
                source_id=row["source_id"] or "",
                visibility=row["visibility"] or "channel",
                content=row["content"] or "",
            ))
        return results

    async def read_bulletin(self, workspace_dir: Path, bulletin_id: str) -> Bulletin | None:
        """Read a specific bulletin by ID."""
        row = await self.db.fetch_one(
            "SELECT * FROM memory_bulletins WHERE id = ?",
            (bulletin_id,),
        )
        if not row:
            return None
        return Bulletin(
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
            channel_id=row["channel_id"] or "",
            source_type=row["source_type"] or "",
            source_id=row["source_id"] or "",
            visibility=row["visibility"] or "channel",
            content=row["content"] or "",
        )

    # ── Session bulletin generation ───────────────────────────────

    async def generate_session_bulletins(
        self,
        workspace_dir: Path,
        session_key: str,
        *,
        active_from: str = "1970-01-01",
        limit: int = 100,
        run_dream: bool = True,
    ) -> dict[str, Any]:
        """Generate bulletins for a session from recent messages.

        Shared by heartbeat idle-summary and /bulletin slash command.
        Returns a summary dict with bulletins generated, errors, etc.
        """
        from cyborg_server.services.memory.bulletin_generator import (
            build_generator_input,
            generate_bulletins,
        )
        from cyborg_server.services.memory.channels import (
            derive_visibility,
            resolve_channel_id,
        )
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        # Find the active-from boundary: last bulletin's session_range_end
        if active_from == "1970-01-01":
            row = await self.db.fetch_one(
                "SELECT MAX(session_range_end) AS active_from FROM memory_bulletins "
                "WHERE source_id = ? AND session_range_end != ''",
                (session_key,),
            )
            active_from = row["active_from"] if row and row["active_from"] else "1970-01-01"

        # Find the last message timestamp (upper bound)
        last_msg = await self.db.fetch_one(
            "SELECT MAX(created_at) AS last_at FROM session_messages "
            "WHERE session_key = ? AND role IN ('user', 'assistant')",
            (session_key,),
        )
        active_to = last_msg["last_at"] if last_msg and last_msg["last_at"] else None
        if not active_to:
            return {"status": "empty", "bulletins_generated": 0, "reason": "no messages"}

        # Load messages in range
        rows = await self.db.fetch_all(
            "SELECT role, content, sender_id, created_at FROM session_messages "
            "WHERE session_key = ? AND created_at > ? AND created_at <= ? "
            "AND role IN ('user', 'assistant') ORDER BY created_at ASC",
            (session_key, active_from, active_to),
        )
        if not rows:
            return {"status": "empty", "bulletins_generated": 0, "reason": "no new messages"}

        messages = [dict(r) for r in rows]
        if sum(len(m.get("content", "") or "") for m in messages) < 50:
            return {"status": "empty", "bulletins_generated": 0, "reason": "content too short"}

        # Build participants from session_participants
        participant_rows = await self.db.fetch_all(
            "SELECT contact_id, identifier, display_name FROM session_participants "
            "WHERE session_key = ?",
            (session_key,),
        )
        contact_to_name: dict[str, str] = {}
        for r in participant_rows:
            name = r["display_name"]
            if name and r["contact_id"]:
                contact_to_name[r["contact_id"]] = name

        # Use canonical contact IDs for participants
        participants = [
            {"id": canonical_contact_id(cid), "name": name}
            for cid, name in contact_to_name.items()
        ]

        gen_input = build_generator_input(
            session_key=session_key,
            messages=[
                {
                    "sender_contact_id": m.get("sender_id", "assistant"),
                    "timestamp": m.get("created_at", ""),
                    "content": (m.get("content") or "")[:500],
                }
                for m in messages[-limit:]
            ],
            participants=participants,
        )

        llm = LLMDispatchService(self.ctx)
        bulletin_texts = await generate_bulletins(llm, gen_input)

        channel_id = resolve_channel_id(session_key)
        visibility = derive_visibility(session_key)

        if not bulletin_texts:
            # Write a digested sentinel so the range isn't reprocessed
            bulletin_id = await self.write_bulletin(
                workspace_dir,
                channel_id=channel_id,
                source_type="session",
                source_id=session_key,
                content="",
                visibility=visibility,
                occurred_at=active_to,
                session_range_start=active_from,
                session_range_end=active_to,
            )
            await self.db.execute(
                "UPDATE memory_bulletins SET digested = 1 WHERE id = ?",
                (bulletin_id,),
            )
            await self.ensure_group_entity(
                workspace_dir, session_key=session_key, bulletin_id=bulletin_id,
            )
            return {"status": "empty", "bulletins_generated": 0, "reason": "nothing memory-worthy"}

        # Write bulletins
        bulletin_ids = []
        for text in bulletin_texts:
            bid = await self.write_bulletin(
                workspace_dir,
                channel_id=channel_id,
                source_type="session",
                source_id=session_key,
                content=text,
                visibility=visibility,
                occurred_at=active_to,
                session_range_start=active_from,
                session_range_end=active_to,
            )
            bulletin_ids.append(bid)
            await self.ensure_group_entity(
                workspace_dir, session_key=session_key, bulletin_id=bid,
            )

        result: dict[str, Any] = {
            "status": "ok",
            "bulletins_generated": len(bulletin_texts),
            "bulletin_ids": bulletin_ids,
            "messages_processed": len(messages),
            "active_from": active_from,
            "active_to": active_to,
        }

        # Optionally run dream pipeline
        if run_dream:
            dream_result = await self.run_dream(workspace_dir)
            result["dream"] = dream_result

        return result

    # ── Entities ──────────────────────────────────────────────────

    async def write_entity(self, workspace_dir: Path, entity: EntityDocument) -> str:
        """Write an entity document to the database."""
        now = utcnow()
        status = entity.status if entity.status in ("active", "archived") else "active"

        await self.db.execute(
            "INSERT OR REPLACE INTO memory_entities "
            "(entity_id, entity_type, display_name, status, extra_frontmatter, "
            "body, source_bulletins, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity.entity_id,
                entity.entity_type,
                entity.display_name,
                status,
                json.dumps(entity.extra_frontmatter),
                entity.body,
                json.dumps(entity.source_bulletins),
                now.isoformat(),
            ),
        )

        # Update relations — parse from body if not in related_entities
        relations = entity.related_entities
        if not any(relations.values()):
            relations = _parse_related_from_body(entity.body)

        await self.db.execute(
            "DELETE FROM memory_entity_relations WHERE source_entity_id = ?",
            (entity.entity_id,),
        )
        if relations:
            params = []
            for cat, ids in relations.items():
                for target_id in ids:
                    if target_id:
                        params.append((entity.entity_id, cat, target_id))
            if params:
                await self.db.execute_many(
                    "INSERT OR IGNORE INTO memory_entity_relations "
                    "(source_entity_id, category, target_entity_id) VALUES (?, ?, ?)",
                    params,
                )

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

        logger.info("Entity written: %s/%s", entity.entity_type, entity.entity_id)
        return entity.entity_id

    async def read_entity(self, workspace_dir: Path, entity_id: str) -> EntityDocument | None:
        """Read an entity document by ID."""
        row = await self.db.fetch_one(
            "SELECT * FROM memory_entities WHERE entity_id = ?",
            (entity_id,),
        )
        if not row:
            return None

        # Load relations
        rel_rows = await self.db.fetch_all(
            "SELECT category, target_entity_id FROM memory_entity_relations "
            "WHERE source_entity_id = ?",
            (entity_id,),
        )
        related: dict[str, list[str]] = {cat: [] for cat in ENTITY_CATEGORIES}
        for r in rel_rows:
            related.setdefault(r["category"], []).append(r["target_entity_id"])

        return EntityDocument(
            entity_id=row["entity_id"],
            entity_type=row["entity_type"],
            display_name=row["display_name"] or "",
            status=row["status"] or "active",
            extra_frontmatter=json.loads(row["extra_frontmatter"]) if row["extra_frontmatter"] else {},
            body=row["body"] or "",
            related_entities=related,
            source_bulletins=json.loads(row["source_bulletins"]) if row["source_bulletins"] else [],
        )

    async def list_entities(self, workspace_dir: Path, entity_type: str) -> list[EntityDocument]:
        """List all entities of a given type."""
        rows = await self.db.fetch_all(
            "SELECT * FROM memory_entities WHERE entity_type = ? ORDER BY entity_id",
            (entity_type,),
        )
        results = []
        for row in rows:
            results.append(EntityDocument(
                entity_id=row["entity_id"],
                entity_type=entity_type,
                display_name=row["display_name"] or "",
                status=row["status"] or "active",
                extra_frontmatter=json.loads(row["extra_frontmatter"]) if row["extra_frontmatter"] else {},
                body=row["body"] or "",
                source_bulletins=json.loads(row["source_bulletins"]) if row["source_bulletins"] else [],
            ))
        return results

    # ── Dream Process ─────────────────────────────────────────────

    async def process_bulletin(self, workspace_dir: Path, bulletin: Bulletin, *, mode: str = "patch") -> dict[str, Any]:
        """Process a single bulletin: extract claims, update entities."""
        from cyborg_server.services.llm_dispatch import LLMDispatchService
        llm = LLMDispatchService(self.ctx)

        # Look up group entity ID for claim extraction hint
        group_entity_id = await self._resolve_group_entity_id(bulletin.source_id)

        # Load contact roster for claim extraction
        directory = await self._get_contact_directory()
        contact_roster = self._format_contact_roster(directory)
        group_members = await self._load_group_members(bulletin.source_id) if group_entity_id else ""
        group_members_str = self._format_group_members(directory, group_members) if group_members else ""

        claims = await extract_claims_from_bulletin(
            llm, bulletin,
            known_group_entity_id=group_entity_id,
            contact_roster=contact_roster,
            group_members=group_members_str,
            db=self.db,
        )
        wrote_claims = 0
        for claim in claims:
            await write_claim(self.db, claim)
            wrote_claims += 1

        entity_result = await self._update_entities_from_claims(llm, claims, mode=mode)

        return {
            "bulletin_id": bulletin.id,
            "claims_extracted": wrote_claims,
            "claims": [
                {"id": c.id, "type": c.type, "subject_id": c.subject_id,
                 "predicate": c.predicate, "object_id": c.object_id, "body": c.body}
                for c in claims
            ],
            "entity_ops": entity_result["count"],
            "entities_updated": entity_result["entity_ids"],
        }

    async def run_dream(self, workspace_dir: Path, *, mode: str = "patch") -> dict[str, Any]:
        """Process all pending (undigested) bulletins through the dream pipeline."""
        bulletins = await self.read_bulletins(workspace_dir, skip_digested=True, oldest_first=True, limit=10000)
        if not bulletins:
            return {"status": "empty", "bulletins_processed": 0}

        logger.info("Memory dream: processing %d bulletins (oldest first)", len(bulletins))
        start = datetime.now().timestamp()

        total_claims = 0
        total_entity_ops = 0
        ops_detail: list[dict[str, Any]] = []

        for bulletin in bulletins:
            result = await self.process_bulletin(workspace_dir, bulletin, mode=mode)
            total_claims += result["claims_extracted"]
            total_entity_ops += result.get("entity_ops", 0)
            ops_detail.append({
                "bulletin": bulletin.id,
                "source": bulletin.source_id or "",
                "claims": result.get("claims", result["claims_extracted"]),
                "entity_ops": result.get("entity_ops", 0),
                "entities_updated": result.get("entities_updated", []),
                "content_preview": (bulletin.content or "")[:120],
            })
            await self._mark_digested(bulletin)

        elapsed = datetime.now().timestamp() - start
        return {
            "status": "completed",
            "bulletins_processed": len(bulletins),
            "bulletin_slugs": [b.id for b in bulletins],
            "claims_extracted": total_claims,
            "entity_ops": total_entity_ops,
            "operations": ops_detail,
            "duration_seconds": round(elapsed, 1),
        }

    async def _update_entities_from_claims(
        self, llm: Any, claims: list[Claim], *, mode: str = "patch"
    ) -> dict[str, Any]:
        """Use LLM to update entity documents from claims — one call per entity."""
        if not claims:
            return {"count": 0, "entity_ids": []}

        from cyborg_server.services.memory.contact_directory import ContactDirectory
        from cyborg_server.services.memory.reconcile import reconcile_contact_id

        directory = None
        if self.ctx and hasattr(self.ctx, "db") and self.ctx.db:
            directory = await ContactDirectory.load(self.ctx.db)
        self._contact_dir_cache = directory

        existing_name_map = await self._index_contact_display_names()

        claims_by_entity: dict[str, list[Claim]] = defaultdict(list)
        for c in claims:
            if not (c.subject_id and isinstance(c.subject_id, str)):
                continue
            sid = normalize_entity_id(c.subject_id)
            display_name = existing_name_map.get(sid, "")
            canonical = reconcile_contact_id(sid, display_name, directory)
            if canonical != sid:
                c.subject_id = canonical
            if isinstance(c.object_id, str):
                oid = normalize_entity_id(c.object_id)
                obj_canonical = reconcile_contact_id(
                    oid, existing_name_map.get(oid, ""), directory
                )
                if obj_canonical != oid:
                    c.object_id = obj_canonical
            claims_by_entity[canonical].append(c)

        all_existing_ids = await self._list_all_entity_ids()
        contact_ids = {eid for eid in claims_by_entity if eid.startswith("contact-")}
        contact_name_map = await self._lookup_contact_names(contact_ids) if contact_ids else {}

        wrote = 0
        entity_ids: list[str] = []
        for entity_id, entity_claims in claims_by_entity.items():
            result = await self._update_single_entity(
                llm, entity_id, entity_claims,
                all_existing_ids=all_existing_ids,
                contact_name_map=contact_name_map,
                mode=mode,
            )
            wrote += result["count"]
            entity_ids.extend(result["entity_ids"])

        return {"count": wrote, "entity_ids": entity_ids}

    async def _index_contact_display_names(self) -> dict[str, str]:
        """Return {entity_id: display_name} for every contact entity."""
        rows = await self.db.fetch_all(
            "SELECT entity_id, display_name FROM memory_entities WHERE entity_type = 'contact'"
        )
        return {r["entity_id"]: r["display_name"] for r in rows if r["display_name"]}

    # ── Patch-based entity updates ────────────────────────────────

    @staticmethod
    def _apply_entity_patches(existing_body: str, patches: list[dict]) -> str:
        """Apply search/replace patch operations to an entity body.

        Returns the modified body. Patches that fail to match are skipped with a warning.
        """
        body = existing_body
        for op in patches:
            action = op.get("action")
            if action == "patch":
                search = op.get("search", "")
                replace = op.get("replace", "")
                if search and search in body:
                    body = body.replace(search, replace, 1)
                elif search:
                    logger.warning("Entity patch: search string not found, skipping")
            elif action == "append":
                section = op.get("section", "")
                content = op.get("content", "")
                if not section or not content:
                    continue
                pattern = re.compile(
                    rf"(## {re.escape(section)}\n(?:.*\n)*)", re.MULTILINE
                )
                m = pattern.search(body)
                if m:
                    body = body[:m.end()] + content + "\n" + body[m.end():]
                else:
                    body += f"\n## {section}\n{content}\n"
            elif action == "create":
                return op.get("content", body)
        return body

    async def _derive_related_from_claims(
        self,
        entity_id: str,
        claims: list[Claim],
        existing_relations: dict[str, list[str]] | None = None,
    ) -> dict[str, list[str]]:
        """Build related_entities from claim subject/object IDs + existing relations."""
        related: dict[str, list[str]] = {cat: [] for cat in ENTITY_CATEGORIES}
        if existing_relations:
            for cat, ids in existing_relations.items():
                if cat in related:
                    related[cat] = list(ids)

        if not claims:
            return related

        peer_ids: set[str] = set()
        for c in claims:
            if c.subject_id and c.subject_id != entity_id:
                peer_ids.add(c.subject_id)
            if c.object_id and c.object_id != entity_id:
                peer_ids.add(c.object_id)

        if not peer_ids:
            return related

        rows = await self.db.fetch_all(
            "SELECT entity_id, entity_type FROM memory_entities WHERE entity_id IN ({})".format(
                ",".join("?" for _ in peer_ids)
            ),
            tuple(peer_ids),
        )
        type_map: dict[str, str] = {r["entity_id"]: r["entity_type"] for r in rows}
        category_map: dict[str, str] = {
            "contact": "contacts", "group": "groups", "channel": "channels",
            "trip": "trips", "location": "locations", "event": "events",
            "task": "tasks", "artifact": "artifacts", "decision": "decisions",
        }

        for pid in peer_ids:
            etype = type_map.get(pid, "")
            cat = category_map.get(etype)
            if cat and pid not in related.get(cat, []):
                related.setdefault(cat, []).append(pid)

        return related

    async def _update_single_entity(
        self,
        llm: Any,
        entity_id: str,
        claims: list[Claim],
        *,
        all_existing_ids: set[str],
        contact_name_map: dict[str, str],
        mode: str = "patch",
    ) -> dict[str, Any]:
        """Update a single entity document from new bulletins and claims.

        mode="patch" uses search/replace operations (default, token-efficient).
        mode="full" outputs complete entity documents (fallback).
        """
        known_ids_hint = ""
        if entity_id in all_existing_ids:
            known_ids_hint = (
                "\n\n## KNOWN ENTITY IDS (use these exactly, do not create new IDs for these)\n\n"
                f"- {entity_id}"
            )

        # Collect new bulletin IDs from claims
        new_bulletin_ids: set[str] = set()
        claims_lines = []
        for c in claims:
            obj = c.object_id if isinstance(c.object_id, str) else (str(c.object_id) if c.object_id else "")
            claims_lines.append(f"- [{c.type}] {c.subject_id} {c.predicate} {obj}")
            new_bulletin_ids.update(c.source_bulletins or [])

        # Fetch new bulletin content
        bulletin_lines = []
        if new_bulletin_ids:
            rows = await self.db.fetch_all(
                "SELECT id, content FROM memory_bulletins WHERE id IN ("
                + ",".join("?" for _ in new_bulletin_ids) + ")",
                tuple(new_bulletin_ids),
            )
            for row in rows:
                bulletin_lines.append(f"[{row['id']}]\n{row['content']}")

        # Read full existing entity body (no truncation)
        existing_raw = await self._read_entity_raw(entity_id)

        # Read existing source_bulletins for accumulation
        existing_source_bids: set[str] = set()
        existing_row = await self.db.fetch_one(
            "SELECT source_bulletins FROM memory_entities WHERE entity_id = ?",
            (entity_id,),
        )
        if existing_row and existing_row["source_bulletins"]:
            existing_source_bids = set(json.loads(existing_row["source_bulletins"]))

        # Accumulated bulletin IDs (deterministic, not from LLM)
        accumulated_bids = sorted(existing_source_bids | new_bulletin_ids)

        # Build prompt
        user_prompt = ""
        if bulletin_lines:
            user_prompt += "## NEW BULLETINS\n\n" + "\n\n".join(bulletin_lines) + "\n\n"

        user_prompt += "## NEW CLAIMS\n\n" + "\n".join(claims_lines)

        if existing_raw:
            user_prompt += f"\n\n## EXISTING ENTITY\n\n{existing_raw}"

        user_prompt += known_ids_hint

        if entity_id in contact_name_map:
            user_prompt += "\n\n## CONTACT NAME MAP\n\n"
            user_prompt += "Use these display names for the corresponding contact IDs:\n"
            user_prompt += f"- {entity_id}: {contact_name_map[entity_id]}\n"

        # Derive related entities from claims + existing relations
        existing_relations = None
        if existing_raw:
            existing_relations = _parse_related_from_body(existing_raw)
        related = await self._derive_related_from_claims(
            entity_id, claims, existing_relations
        )

        is_new = not existing_raw

        # Choose prompt and max_tokens based on mode
        if mode == "patch" and not is_new:
            system_prompt = ENTITY_PATCH_PROMPT
            max_tokens = 1000
        else:
            system_prompt = ENTITY_UPDATE_PROMPT
            max_tokens = 3000

        response = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=llm.memory_model,
            call_category="memory_entity_update",
            temperature=0.3,
            max_tokens=max_tokens,
        )

        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            operations = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Entity update: failed to parse LLM response for %s", entity_id)
            return {"count": 0, "entity_ids": []}

        if not isinstance(operations, list):
            return {"count": 0, "entity_ids": []}

        wrote = 0
        written_ids: list[str] = []

        if mode == "patch" and not is_new:
            # Patch mode: apply search/replace operations to existing body
            patched_body = self._apply_entity_patches(existing_raw, operations)
            if patched_body != existing_raw:
                entity = EntityDocument(
                    entity_id=entity_id,
                    entity_type=_detect_entity_type(entity_id),
                    display_name=_extract_display_name(patched_body) or entity_id,
                    status="active",
                    body=patched_body,
                    related_entities=related,
                    source_bulletins=accumulated_bids,
                )
                await self.write_entity(Path("."), entity)
                wrote += 1
                written_ids.append(entity_id)
        else:
            # Full mode or new entity: parse complete documents from LLM output
            for op in operations:
                if not isinstance(op, dict):
                    continue
                action = op.get("action")
                if action in ("write_entity", "create", "update"):
                    content = op.get("content", "")
                    if content:
                        entity = self._parse_entity_from_llm_output(
                            op, entity_id, content
                        )
                        if entity and entity.entity_id and entity.entity_type:
                            entity.source_bulletins = accumulated_bids
                            entity.related_entities = related
                            await self.write_entity(Path("."), entity)
                            wrote += 1
                            written_ids.append(entity.entity_id)
        return {"count": wrote, "entity_ids": written_ids}

    def _parse_entity_from_llm_output(
        self, op: dict, canonical_entity_id: str, content: str
    ) -> EntityDocument | None:
        """Parse an entity document from LLM output, handling frontmatter."""
        try:
            fm, body = parse_frontmatter(content)
        except Exception:
            logger.warning("Entity update: failed to parse frontmatter from LLM output")
            return None

        if not fm:
            return EntityDocument(
                entity_id=op.get("entity_id", ""),
                entity_type=op.get("entity_type", ""),
                display_name=op.get("display_name", ""),
                status=op.get("status", "active"),
                extra_frontmatter=op.get("extra_frontmatter", {}),
                body=op.get("body", content),
                related_entities=op.get("related_entities", {}),
                source_bulletins=op.get("source_bulletins", []),
            )

        eid = fm.get("entity_id", op.get("entity_id", ""))
        etype = fm.get("entity_type", op.get("entity_type", ""))
        if not eid or not etype:
            return None

        # Normalize
        normalized = normalize_entity_id(eid, etype)
        if normalized != eid:
            fm["entity_id"] = normalized
            eid = normalized

        # Override LLM's entity_id with canonical if reconciled
        if (
            etype == "contact"
            and canonical_entity_id
            and canonical_entity_id != eid
            and canonical_entity_id.startswith("contact-")
        ):
            fm["entity_id"] = canonical_entity_id
            eid = canonical_entity_id

        # Enrich with contact data
        eid = self._enrich_contact_frontmatter_inplace(etype, eid, fm)

        extra = {k: v for k, v in fm.items()
                 if k not in {"entity_id", "entity_type", "display_name", "status"}}

        return EntityDocument(
            entity_id=eid,
            entity_type=etype,
            display_name=fm.get("display_name", ""),
            status=fm.get("status", "active"),
            extra_frontmatter=extra,
            body=body,
            source_bulletins=op.get("source_bulletins", []),
        )

    async def _read_entity_raw(self, entity_id: str) -> str | None:
        """Read raw entity body text."""
        row = await self.db.fetch_one(
            "SELECT body FROM memory_entities WHERE entity_id = ?",
            (entity_id,),
        )
        return row["body"] if row else None

    async def _list_all_entity_ids(self) -> set[str]:
        """List all entity IDs."""
        rows = await self.db.fetch_all("SELECT entity_id FROM memory_entities")
        return {r["entity_id"] for r in rows}

    def _enrich_contact_frontmatter_inplace(
        self, etype: str, eid: str, fm: dict
    ) -> str:
        """Enrich contact frontmatter with data from ContactDirectory cache."""
        if etype != "contact" or not eid.startswith("contact-"):
            return eid
        if not re.match(r"^contact-[a-f0-9]{8}$", eid):
            return eid
        cache = getattr(self, "_contact_dir_cache", None)
        if cache is None:
            return eid
        record = cache.get_by_canonical_id(eid)
        if record is None:
            return eid
        fm["contact_id"] = record.uuid
        if record.email:
            fm["email"] = record.email
        if record.phone_number:
            fm["phone_number"] = record.phone_number
        return eid

    async def _lookup_contact_names(self, contact_ids: set[str]) -> dict[str, str]:
        """Look up display names for contact IDs from the database."""
        if not contact_ids:
            return {}
        result = {}
        for cid in contact_ids:
            hex8 = cid.removeprefix("contact-")[:8]
            row = await self.db.fetch_one(
                "SELECT name FROM contacts WHERE id LIKE ? LIMIT 1",
                (f"{hex8}%",),
            )
            if row and row["name"]:
                result[cid] = row["name"]
        return result

    # ── Retrieval ─────────────────────────────────────────────────

    async def _extract_search_terms(self, query: str) -> dict[str, Any]:
        """Use a fast LLM to extract FTS keywords and entity type hints from a query."""
        from cyborg_server.services.llm_dispatch import LLMDispatchService
        llm = LLMDispatchService(self.ctx)
        response = await llm.chat(
            messages=[
                {"role": "system", "content": (
                    "Extract search terms from the user's memory query.\n"
                    "Return JSON: {\"keywords\": [str], \"entity_type\": str or null}\n"
                    "Rules:\n"
                    "- keywords: 1-5 terms most likely to match entity content. "
                    "Include synonyms, related terms, and any names/places mentioned. "
                    "Do NOT include stop words (the, is, a, what, where, who, etc.).\n"
                    "- entity_type: if the query clearly implies a type "
                    "(contact, group, channel, trip, location, event, task, artifact, decision), "
                    "suggest it. Otherwise null.\n"
                    "- Be concise. No explanation."
                )},
                {"role": "user", "content": query},
            ],
            model=llm.memory_model,
            call_category="memory_search_extract",
            temperature=0.0,
            max_tokens=100,
        )
        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"keywords": [], "entity_type": None}

    async def search_entries(
        self, workspace_dir: Path, query: str, entity_type: str = ""
    ) -> dict[str, Any]:
        """Search memory using LLM query extraction → FTS5 → LLM semantic ranking."""
        # Stage 0: Extract search terms via LLM
        extracted = await self._extract_search_terms(query)
        keywords = extracted.get("keywords") or []
        # Strip any remaining FTS5-special chars from LLM output
        keywords = [re.sub(r'[?"*^:(){}[\]\\]', '', k) for k in keywords if k]
        hinted_type = extracted.get("entity_type")
        effective_type = entity_type or hinted_type

        # Stage 1: FTS5 with LLM-extracted keywords
        if keywords:
            fts_query = " OR ".join(f'"{w}"' for w in keywords)
            if effective_type:
                rows = await self.db.fetch_all(
                    "SELECT e.entity_id, e.entity_type, e.display_name, e.body "
                    "FROM memory_entities_fts f "
                    "JOIN memory_entities e ON e.rowid = f.rowid "
                    "WHERE memory_entities_fts MATCH ? AND e.entity_type = ? "
                    "ORDER BY rank LIMIT 20",
                    (fts_query, effective_type),
                )
            else:
                rows = await self.db.fetch_all(
                    "SELECT e.entity_id, e.entity_type, e.display_name, e.body "
                    "FROM memory_entities_fts f "
                    "JOIN memory_entities e ON e.rowid = f.rowid "
                    "WHERE memory_entities_fts MATCH ? "
                    "ORDER BY rank LIMIT 20",
                    (fts_query,),
                )
        else:
            rows = []

        # Fallback: if FTS returns nothing, try the full scan
        if not rows:
            if effective_type:
                rows = await self.db.fetch_all(
                    "SELECT entity_id, entity_type, display_name, body "
                    "FROM memory_entities WHERE entity_type = ?",
                    (effective_type,),
                )
            else:
                rows = await self.db.fetch_all(
                    "SELECT entity_id, entity_type, display_name, body "
                    "FROM memory_entities"
                )

        if not rows:
            return {"abstract": "No memory entries found.", "results": []}

        all_entries = [
            {
                "entity_id": r["entity_id"],
                "entity_type": r["entity_type"],
                "display_name": r["display_name"],
                "body": r["body"] or "",
            }
            for r in rows
        ]

        catalog_lines = []
        for i, entry in enumerate(all_entries):
            catalog_lines.append(
                f"[{i}] {entry['entity_id']} ({entry['entity_type']})\n"
                f"    Name: {entry['display_name']}\n"
                f"    Content: {entry['body']}"
            )

        system_prompt = (
            "You are a memory search agent. Given a query and a catalog of memory entities, "
            "find entities relevant to the query by meaning.\n\n"
            "Return JSON: {\"abstract\": str, \"results\": [{\"index\": int, \"relevance\": str}]}\n"
            "Return {\"abstract\": \"No matches.\", \"results\": []} if nothing matches."
        )
        user_prompt = f"Query: {query}\n\nCatalog:\n" + "\n\n".join(catalog_lines)

        from cyborg_server.services.llm_dispatch import LLMDispatchService
        llm = LLMDispatchService(self.ctx)
        response = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=llm.memory_model,
            call_category="memory_search",
            temperature=0.0,
            max_tokens=600,
        )

        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"abstract": "Search failed.", "results": []}

        results = []
        for item in parsed.get("results", []):
            idx = item.get("index") if isinstance(item, dict) else item
            if isinstance(idx, int) and 0 <= idx < len(all_entries):
                entry = all_entries[idx]
                results.append({
                    "entity_id": entry["entity_id"],
                    "entity_type": entry["entity_type"],
                    "display_name": entry["display_name"],
                    "path": entry["entity_id"],
                    "relevance": item.get("relevance", "") if isinstance(item, dict) else "",
                })

        return {"abstract": parsed.get("abstract", ""), "results": results}

    async def build_memory_index(self, workspace_dir: Path) -> str:
        """Build compact memory index for system prompt injection."""
        return await build_memory_index_text_db(self.db)

    async def rebuild_fts(self) -> int:
        """Rebuild the FTS5 index from scratch. Returns row count."""
        await self.db.execute("DELETE FROM memory_entities_fts")
        await self.db.execute(
            "INSERT INTO memory_entities_fts(rowid, entity_id, display_name, body) "
            "SELECT rowid, entity_id, display_name, body FROM memory_entities"
        )
        row = await self.db.fetch_one("SELECT count(*) AS c FROM memory_entities_fts")
        return row["c"] if row else 0

    # ── Person/Contact helpers ────────────────────────────────────

    async def _get_contact_directory(self):
        """Load and cache ContactDirectory."""
        from cyborg_server.services.memory.contact_directory import ContactDirectory
        cache = getattr(self, "_contact_dir_cache", None)
        if cache is None and self.ctx and hasattr(self.ctx, "db") and self.ctx.db:
            cache = await ContactDirectory.load(self.ctx.db)
            self._contact_dir_cache = cache
        return cache

    @staticmethod
    def _format_contact_roster(directory: Any) -> str:
        """Format ContactDirectory as a roster string for the LLM prompt."""
        if directory is None:
            return ""
        lines = []
        for record in directory._by_canonical.values():
            lines.append(f"- {record.canonical_id}: {record.name}")
        return "\n".join(lines)

    async def _load_group_members(self, source_id: str) -> list[str] | None:
        """Load group member canonical contact IDs for a session."""
        if not source_id:
            return None
        route = await self.db.fetch_one(
            "SELECT chat_id, kind FROM session_routes WHERE session_key = ?",
            (source_id,),
        )
        if not route or route["kind"] != "group" or not route["chat_id"]:
            return None
        rows = await self.db.fetch_all(
            "SELECT gm.contact_id FROM whatsappgroup_members gm "
            "JOIN contacts c ON c.id = gm.contact_id "
            "WHERE gm.group_id = (SELECT id FROM whatsappgroups WHERE whatsapp_jid = ?) "
            "AND gm.left_at IS NULL",
            (route["chat_id"],),
        )
        return [f"contact-{str(r['contact_id'])[:8]}" for r in rows]

    @staticmethod
    def _format_group_members(directory: Any, member_ids: list[str]) -> str:
        """Format group member list for the LLM prompt."""
        if not member_ids or directory is None:
            return ""
        parts = []
        for mid in member_ids:
            record = directory.get_by_canonical_id(mid)
            name = record.name if record else mid
            parts.append(f"{mid} ({name})")
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
        """Create a minimal contact entity document if one doesn't exist."""
        canonical_id = canonical_contact_id(contact_id)
        existing = await self.read_entity(workspace_dir, canonical_id)
        if existing:
            return canonical_id

        contact_lines = []
        if phone_number:
            contact_lines.append(f"- Phone: {phone_number}")
        if email:
            contact_lines.append(f"- Email: {email}")
        if channel:
            contact_lines.append(f"- Channel: {channel}")

        body = f"# {name}\n\n## Summary\n\nContact via {channel or 'unknown'}.\n\n## Contact\n\n" + "\n".join(contact_lines)

        entity = EntityDocument(
            entity_id=canonical_id,
            entity_type="contact",
            display_name=name,
            status="active",
            extra_frontmatter={"contact_source": "contacts_db", "contact_id": contact_id},
            body=body,
        )
        await self.write_entity(workspace_dir, entity)
        return canonical_id

    async def find_person_entry(
        self,
        workspace_dir: Path,
        *,
        contact_id: str = "",
        name: str = "",
    ) -> str | None:
        """Find a contact entity by ID. Returns full content or None."""
        if contact_id:
            canonical_id = canonical_contact_id(contact_id)
            entity = await self.read_entity(workspace_dir, canonical_id)
            if entity:
                return serialize_frontmatter(
                    {"entity_id": entity.entity_id, "entity_type": entity.entity_type,
                     "display_name": entity.display_name, **entity.extra_frontmatter},
                    entity.body,
                )
        return None

    # ── Group helpers ─────────────────────────────────────────────

    async def _resolve_group_entity_id(self, source_id: str) -> str | None:
        """Look up the group entity ID for a bulletin's source session.

        Returns None if not a group session or no entity has been created yet.
        """
        if not source_id:
            return None
        route = await self.db.fetch_one(
            "SELECT chat_id, kind FROM session_routes WHERE session_key = ?",
            (source_id,),
        )
        if not route or route["kind"] != "group" or not route["chat_id"]:
            return None
        row = await self.db.fetch_one(
            "SELECT memory_entity_id FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (route["chat_id"],),
        )
        return row["memory_entity_id"] if row and row["memory_entity_id"] else None

    async def ensure_group_entity(
        self,
        workspace_dir: Path,
        session_key: str,
        bulletin_id: str,
    ) -> str | None:
        """Ensure a group entity exists for a group session and link the bulletin.

        Resolves via session_routes → whatsappgroups.memory_entity_id.
        Creates the entity if needed and links the bulletin via memory_entity_bulletins.
        Returns the group entity ID, or None if not a group session.
        """
        # 1. Look up session route to get chat_id
        route = await self.db.fetch_one(
            "SELECT chat_id, kind FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        if not route or route["kind"] != "group" or not route["chat_id"]:
            return None

        chat_id = route["chat_id"]

        # 2. Look up whatsappgroup
        group_row = await self.db.fetch_one(
            "SELECT id, name, description, memory_entity_id, member_count "
            "FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (chat_id,),
        )
        if not group_row:
            return None

        group_name = group_row["name"] or chat_id
        group_desc = group_row["description"] or ""
        existing_entity_id = group_row["memory_entity_id"]

        # 3. If entity ID already recorded, verify it exists
        if existing_entity_id:
            entity = await self.read_entity(workspace_dir, existing_entity_id)
            if entity and entity.display_name != group_name:
                entity.display_name = group_name
                await self.write_entity(workspace_dir, entity)
        else:
            # 4. Create the group entity
            entity_id = f"group-{uuid.uuid4().hex[:8]}"
            body = f"# {group_name}\n\n## Summary\n\nWhatsApp group."
            if group_desc:
                body += f"\n\n## Description\n\n{group_desc}"
            body += f"\n\n## Details\n\n- WhatsApp JID: {chat_id}"

            entity = EntityDocument(
                entity_id=entity_id,
                entity_type="group",
                display_name=group_name,
                status="active",
                extra_frontmatter={"whatsapp_jid": chat_id},
                body=body,
            )
            await self.write_entity(workspace_dir, entity)
            existing_entity_id = entity_id

            # 5. Store entity ID on whatsappgroups
            await self.db.execute(
                "UPDATE whatsappgroups SET memory_entity_id = ? WHERE id = ?",
                (entity_id, group_row["id"]),
            )

        # 6. Link bulletin to group entity
        await self.db.execute(
            "INSERT OR IGNORE INTO memory_entity_bulletins (entity_id, bulletin_id) VALUES (?, ?)",
            (existing_entity_id, bulletin_id),
        )

        return existing_entity_id

    # ── Rebuild ───────────────────────────────────────────────────

    async def rebuild(self, workspace_dir: Path, *, entity_id: str | None = None, all: bool = False, mode: str = "patch") -> dict[str, Any]:
        """Rebuild derived data from bulletins."""
        if entity_id:
            return await self._rebuild_entity(workspace_dir, entity_id, mode=mode)

        if all:
            await self.db.execute("DELETE FROM memory_claims")
            await self.db.execute("DELETE FROM memory_claim_bulletins")
            await self.db.execute("DELETE FROM memory_entity_relations")
            await self.db.execute("DELETE FROM memory_entity_bulletins")
            await self.db.execute("DELETE FROM memory_aliases")
            await self.db.execute("UPDATE memory_bulletins SET digested = 0")

            bulletins = await self.read_bulletins(workspace_dir, limit=10000, oldest_first=True)
            total_claims = 0
            for bulletin in bulletins:
                result = await self.process_bulletin(workspace_dir, bulletin, mode=mode)
                total_claims += result["claims_extracted"]

            return {"status": "completed", "bulletins_processed": len(bulletins), "claims": total_claims}

        return {"status": "no_op"}

    async def _rebuild_entity(self, workspace_dir: Path, entity_id: str, *, mode: str = "patch") -> dict[str, Any]:
        """Rebuild a single entity by reprocessing its linked bulletins."""
        # Find bulletins linked to this entity
        rows = await self.db.fetch_all(
            "SELECT bulletin_id FROM memory_entity_bulletins WHERE entity_id = ?",
            (entity_id,),
        )
        bulletin_ids = [r["bulletin_id"] for r in rows]
        if not bulletin_ids:
            return {"status": "no_op", "reason": "no linked bulletins"}

        # Delete claims sourced from these bulletins
        await self.db.execute(
            "DELETE FROM memory_claims WHERE source_bulletins IN ({})".format(
                ",".join("?" for _ in bulletin_ids)
            ),
            tuple([json.dumps(bids) if i == 0 else json.dumps(bids) for i, bids in enumerate(bulletin_ids)]),
        )
        # Broader approach: delete claims where source_bulletins JSON contains any of these IDs
        for bid in bulletin_ids:
            await self.db.execute(
                "DELETE FROM memory_claims WHERE source_bulletins LIKE ?",
                (f'%{bid}%',),
            )

        # Delete the entity itself so it gets fully recreated
        await self.db.execute("DELETE FROM memory_entity_relations WHERE source_entity_id = ?", (entity_id,))
        await self.db.execute("DELETE FROM memory_entity_bulletins WHERE entity_id = ?", (entity_id,))
        await self.db.execute("DELETE FROM memory_aliases WHERE entity_id = ?", (entity_id,))
        await self.db.execute("DELETE FROM memory_entities WHERE entity_id = ?", (entity_id,))

        # Mark bulletins as undigested so they'll be reprocessed
        await self.db.execute(
            "UPDATE memory_bulletins SET digested = 0 WHERE id IN ({})".format(
                ",".join("?" for _ in bulletin_ids)
            ),
            tuple(bulletin_ids),
        )

        # Re-read bulletins and reprocess
        bulletins = await self.read_bulletins(workspace_dir, limit=10000, skip_digested=True, oldest_first=True)
        # Filter to only those linked to this entity
        bulletin_set = set(bulletin_ids)
        relevant = [b for b in bulletins if b.id in bulletin_set]
        if not relevant:
            return {"status": "no_op", "reason": "no undigested bulletins found"}

        total_claims = 0
        for bulletin in relevant:
            result = await self.process_bulletin(workspace_dir, bulletin, mode=mode)
            total_claims += result["claims_extracted"]

        return {"status": "completed", "entity_id": entity_id, "bulletins_processed": len(relevant), "claims": total_claims}

    # ── Validation ────────────────────────────────────────────────

    async def validate(self, workspace_dir: Path) -> dict[str, Any]:
        """Validate memory data: check for missing fields."""
        issues: list[str] = []
        rows = await self.db.fetch_all(
            "SELECT entity_id FROM memory_entities WHERE display_name = '' OR entity_type = ''"
        )
        for r in rows:
            issues.append(f"{r['entity_id']}: missing display_name or entity_type")
        return {"valid": len(issues) == 0, "issues": issues}

    # ── Legacy compatibility ──────────────────────────────────────

    async def browse_category(self, workspace_dir: Path, wiki: str, category: str) -> list[dict[str, Any]]:
        """Legacy: browse entities by type (category maps to entity_type)."""
        return [
            {"slug": e.entity_id, "title": e.display_name, "modified": 0}
            for e in await self.list_entities(workspace_dir, category)
        ]

    async def read_entry(self, workspace_dir: Path, wiki: str, category: str, slug: str) -> str | None:
        """Legacy: read an entity by wiki/category/slug."""
        entity = await self.read_entity(workspace_dir, slug)
        if entity:
            return serialize_frontmatter(
                {"entity_id": entity.entity_id, "entity_type": entity.entity_type,
                 "display_name": entity.display_name, **entity.extra_frontmatter},
                entity.body,
            )
        return None

    async def write_entry(self, workspace_dir: Path, wiki: str, category: str, slug: str, title: str, content: str) -> str:
        """Legacy: write an entity. Category maps to entity_type."""
        entity = EntityDocument(
            entity_id=slug,
            entity_type=category,
            display_name=title,
            body=content,
        )
        return await self.write_entity(workspace_dir, entity)

    async def list_recent_entries(self, workspace_dir: Path, wiki_names: list[str], limit: int = 50) -> dict[str, Any]:
        """Legacy: list recent entity documents."""
        rows = await self.db.fetch_all(
            "SELECT entity_id, entity_type, display_name, body, updated_at "
            "FROM memory_entities ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        entries = []
        for r in rows:
            summary = ""
            for line in (r["body"] or "").splitlines():
                if line.strip() and not line.strip().startswith("#"):
                    summary = line.strip()[:80]
                    break
            entries.append({
                "path": r["entity_id"],
                "wiki": "core",
                "category": r["entity_type"],
                "slug": r["entity_id"],
                "title": r["display_name"] or "",
                "summary": summary,
                "modified": r["updated_at"],
            })
        return {
            "stats": {"total_entries": len(rows)},
            "recent": entries,
        }

    @staticmethod
    async def _build_memory_index_static(workspace_dir: Path, wiki_names: list[str]) -> str:
        """Build a compact memory index without a service instance."""
        return ""  # Requires db access — use build_memory_index_text_db instead

    def rebuild_wiki_index(self, workspace_dir: Path, wiki_name: str) -> None:
        """Legacy: no-op, indexes are maintained by write operations."""

    async def resolve_accessible_wikis(self, workspace_dir: Path, session_key: str | None = None) -> list[str]:
        return ["core"]

    async def resolve_writable_wikis(self, workspace_dir: Path, session_key: str | None = None) -> list[str]:
        return ["core"]

    def validate_wiki_category(self, workspace_dir: Path, wiki: str, category: str) -> bool:
        return True

    def move_to_digested(self, workspace_dir: Path, bulletin_paths: list[Path]) -> None:
        """Legacy: no-op."""

    async def _mark_digested(self, bulletin: Bulletin) -> None:
        """Mark a bulletin as digested."""
        await self.db.execute(
            "UPDATE memory_bulletins SET digested = 1 WHERE id = ?",
            (bulletin.id,),
        )


async def build_memory_index_text_db(db: Any) -> str:
    """Build a compact memory index from the database for system prompt injection."""
    rows = await db.fetch_all(
        "SELECT entity_type, entity_id, display_name, substr(body, 1, 80) AS body_preview "
        "FROM memory_entities ORDER BY entity_type, entity_id"
    )
    if not rows:
        return ""

    by_type: dict[str, list[str]] = {}
    for r in rows:
        entry_str = r["display_name"] or r["entity_id"]
        preview = (r["body_preview"] or "").split("\n")
        summary = ""
        for line in preview:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                summary = stripped[:80]
                break
        if summary:
            entry_str += f" — {summary}"
        by_type.setdefault(r["entity_type"], []).append(entry_str)

    lines = [f"**{t}**: " + ", ".join(entries) for t, entries in sorted(by_type.items())]
    return "\n".join(lines)
