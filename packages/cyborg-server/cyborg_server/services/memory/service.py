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
from cyborg_server.services.memory.prompts import ENTITY_UPDATE_PROMPT

logger = logging.getLogger(__name__)

_RELATED_ENTITIES_RE = re.compile(
    r"## Related Entities\n+(.*?)(?=\n## |\Z)", re.DOTALL
)


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
        query += " ORDER BY created_at DESC LIMIT ?"
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

    async def process_bulletin(self, workspace_dir: Path, bulletin: Bulletin) -> dict[str, Any]:
        """Process a single bulletin: extract claims, update entities."""
        from cyborg_server.services.llm_dispatch import LLMDispatchService
        llm = LLMDispatchService(self.ctx)

        claims = await extract_claims_from_bulletin(llm, bulletin)
        wrote_claims = 0
        for claim in claims:
            await write_claim(self.db, claim)
            wrote_claims += 1

        entity_result = await self._update_entities_from_claims(llm, claims)

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

    async def run_dream(self, workspace_dir: Path) -> dict[str, Any]:
        """Process all pending (undigested) bulletins through the dream pipeline."""
        bulletins = await self.read_bulletins(workspace_dir, skip_digested=True)
        if not bulletins:
            return {"status": "empty", "bulletins_processed": 0}

        logger.info("Memory dream: processing %d bulletins", len(bulletins))
        start = datetime.now().timestamp()

        total_claims = 0
        total_entity_ops = 0
        ops_detail: list[dict[str, Any]] = []

        for bulletin in bulletins:
            result = await self.process_bulletin(workspace_dir, bulletin)
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
        self, llm: Any, claims: list[Claim]
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

    async def _update_single_entity(
        self,
        llm: Any,
        entity_id: str,
        claims: list[Claim],
        *,
        all_existing_ids: set[str],
        contact_name_map: dict[str, str],
    ) -> dict[str, Any]:
        """Update a single entity document from new bulletins and claims."""
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

        response = await llm.chat(
            messages=[
                {"role": "system", "content": ENTITY_UPDATE_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            model=llm.memory_model,
            call_category="memory_entity_update",
            temperature=0.3,
            max_tokens=3000,
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
                        # Stamp accumulated bulletin IDs (code, not LLM)
                        entity.source_bulletins = accumulated_bids
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

    async def search_entries(
        self, workspace_dir: Path, query: str, entity_type: str = ""
    ) -> dict[str, Any]:
        """Search memory using entity documents."""
        if entity_type:
            rows = await self.db.fetch_all(
                "SELECT entity_id, entity_type, display_name, substr(body, 1, 500) AS body_preview "
                "FROM memory_entities WHERE entity_type = ?",
                (entity_type,),
            )
        else:
            rows = await self.db.fetch_all(
                "SELECT entity_id, entity_type, display_name, substr(body, 1, 500) AS body_preview "
                "FROM memory_entities"
            )

        if not rows:
            return {"abstract": "No memory entries found.", "results": []}

        all_entries = [
            {
                "entity_id": r["entity_id"],
                "entity_type": r["entity_type"],
                "display_name": r["display_name"],
                "body": r["body_preview"] or "",
            }
            for r in rows
        ]

        catalog_lines = []
        for i, entry in enumerate(all_entries):
            catalog_lines.append(
                f"[{i}] {entry['entity_id']} ({entry['entity_type']})\n"
                f"    Name: {entry['display_name']}\n"
                f"    Content: {entry['body'][:300]}"
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

    # ── Person/Contact helpers ────────────────────────────────────

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

    # ── Rebuild ───────────────────────────────────────────────────

    async def rebuild(self, workspace_dir: Path, *, entity_id: str | None = None, all: bool = False) -> dict[str, Any]:
        """Rebuild derived data from bulletins."""
        if all:
            await self.db.execute("DELETE FROM memory_claims")
            await self.db.execute("DELETE FROM memory_claim_bulletins")
            await self.db.execute("DELETE FROM memory_entity_relations")
            await self.db.execute("DELETE FROM memory_entity_bulletins")
            await self.db.execute("DELETE FROM memory_aliases")
            await self.db.execute("UPDATE memory_bulletins SET digested = 0")

            bulletins = await self.read_bulletins(workspace_dir, limit=10000)
            total_claims = 0
            for bulletin in bulletins:
                result = await self.process_bulletin(workspace_dir, bulletin)
                total_claims += result["claims_extracted"]

            return {"status": "completed", "bulletins_processed": len(bulletins), "claims": total_claims}

        return {"status": "no_op"}

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
