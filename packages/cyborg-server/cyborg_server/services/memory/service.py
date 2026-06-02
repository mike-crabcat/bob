"""MemoryService v6 — channel-centric memory system."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from cyborg_server.services.base import BaseService, utcnow
from cyborg_server.services.memory.models import (
    ENTITY_CATEGORIES,
    ENTITY_TYPES,
    Bulletin,
    Claim,
    EntityDocument,
    EntityRef,
    QueryContext,
    parse_frontmatter,
    serialize_frontmatter,
)
from cyborg_server.services.memory.channels import (
    derive_channel_type,
    resolve_channel_id,
)
from cyborg_server.services.memory.claim_service import (
    extract_claims_from_bulletin,
    get_active_claims,
    get_all_claims,
    read_claim,
    write_claim,
)
from cyborg_server.services.memory.entity_resolver import (
    canonical_contact_id,
    load_aliases,
    normalize_entity_id,
    resolve_contact,
)
from cyborg_server.services.memory.index_service import (
    build_memory_index_text,
    rebuild_all as rebuild_indexes,
)
from cyborg_server.services.memory.prompts import (
    ENTITY_UPDATE_PROMPT,
    MEMORY_INDEX_HEADER,
)

logger = logging.getLogger(__name__)


class MemoryService(BaseService):
    """Reads and writes v6 memory: bulletins, claims, entities."""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)

    @staticmethod
    def _memory_dir(workspace_dir: Path) -> Path:
        return workspace_dir.expanduser() / "memory"

    # ── Setup ─────────────────────────────────────────────────────

    @staticmethod
    def ensure_memory_structure(workspace_dir: Path) -> None:
        """Create v6 memory directory structure."""
        memory_dir = MemoryService._memory_dir(workspace_dir)
        if not memory_dir.is_dir():
            memory_dir.mkdir(parents=True, exist_ok=True)

        subdirs = (
            ["bulletins", "claims", "aliases", "indexes", "summaries", "policies"]
            + [f"entities/{t}" for t in ENTITY_TYPES]
        )
        for sub in subdirs:
            (memory_dir / sub).mkdir(parents=True, exist_ok=True)

    # ── Bulletins ─────────────────────────────────────────────────

    def write_bulletin(
        self,
        workspace_dir: Path,
        *,
        channel_id: str,
        source_type: str,
        source_id: str = "",
        visibility: str = "private",
        scope: list[str] | None = None,
        entities: dict[str, list] | None = None,
        content: str,
    ) -> str:
        """Write an immutable bulletin to the date-organized store."""
        memory_dir = self._memory_dir(workspace_dir)
        now = utcnow()

        # Generate bulletin ID
        date_str = now.strftime("%Y-%m-%d")
        slug = f"bulletin-{date_str}-{uuid.uuid4().hex[:6]}"

        # Date-organized path
        month_dir = memory_dir / "bulletins" / now.strftime("%Y") / now.strftime("%m")
        month_dir.mkdir(parents=True, exist_ok=True)

        fm = {
            "id": slug,
            "created_at": now.isoformat(),
            "channel_id": channel_id,
            "source_type": source_type,
            "source_id": source_id,
            "visibility": visibility,
            "scope": scope or [],
            "entities": entities or {},
        }

        body = f"# Update\n\n{content}"
        path = month_dir / f"{slug}.md"
        path.write_text(serialize_frontmatter(fm, body), encoding="utf-8")
        logger.info("Bulletin written: %s", slug)
        return str(path.relative_to(workspace_dir))

    def read_bulletins(
        self,
        workspace_dir: Path,
        *,
        limit: int = 50,
        from_date: str | None = None,
        skip_digested: bool = False,
    ) -> list[Bulletin]:
        """Read bulletins from the date-organized store."""
        memory_dir = self._memory_dir(workspace_dir)
        bulletins_dir = memory_dir / "bulletins"
        if not bulletins_dir.is_dir():
            return []

        results: list[Bulletin] = []
        files = sorted(bulletins_dir.rglob("*.md"), reverse=True)

        for md_file in files:
            if len(results) >= limit:
                break
            fm, body = parse_frontmatter(md_file.read_text(encoding="utf-8"))
            if not fm or not fm.get("id"):
                continue
            if skip_digested and fm.get("digested"):
                continue
            # Extract content (strip # Update header)
            content = body.strip()
            if content.startswith("# Update"):
                content = content[len("# Update"):].strip()

            # Parse entity refs
            raw_entities = fm.get("entities", {})
            entities: dict[str, list[EntityRef]] = {}
            for cat in ENTITY_CATEGORIES:
                raw_list = raw_entities.get(cat, [])
                if isinstance(raw_list, list):
                    parsed: list[EntityRef] = []
                    for r in raw_list:
                        if isinstance(r, str):
                            parsed.append(EntityRef(id=r))
                        elif isinstance(r, dict):
                            parsed.append(EntityRef(
                                id=r.get("id", str(r)),
                                display_name=r.get("display_name"),
                                resolution_status=r.get("resolution_status", "known"),
                                role=r.get("role"),
                            ))
                    entities[cat] = parsed

            results.append(Bulletin(
                id=fm["id"],
                created_at=datetime.fromisoformat(fm["created_at"]) if "created_at" in fm else datetime.now(),
                channel_id=fm.get("channel_id", ""),
                source_type=fm.get("source_type", ""),
                source_id=fm.get("source_id", ""),
                visibility=fm.get("visibility", "private"),
                scope=fm.get("scope", []),
                entities=entities,
                content=content,
            ))

        return results

    def read_bulletin(self, workspace_dir: Path, bulletin_id: str) -> Bulletin | None:
        """Read a specific bulletin by ID."""
        bulletins = self.read_bulletins(workspace_dir, limit=1000)
        for b in bulletins:
            if b.id == bulletin_id:
                return b
        return None

    # ── Entities ──────────────────────────────────────────────────

    def write_entity(self, workspace_dir: Path, entity: EntityDocument) -> str:
        """Write an entity document to disk."""
        memory_dir = self._memory_dir(workspace_dir)
        entity_dir = memory_dir / "entities" / entity.entity_type
        entity_dir.mkdir(parents=True, exist_ok=True)

        fm = {
            "entity_id": entity.entity_id,
            "entity_type": entity.entity_type,
            "display_name": entity.display_name,
            "status": entity.status,
            **entity.extra_frontmatter,
        }

        path = entity_dir / f"{entity.entity_id}.md"
        path.write_text(serialize_frontmatter(fm, entity.body), encoding="utf-8")
        logger.info("Entity written: %s/%s", entity.entity_type, entity.entity_id)
        return str(path.relative_to(workspace_dir))

    def read_entity(self, workspace_dir: Path, entity_id: str) -> EntityDocument | None:
        """Read an entity document by ID."""
        memory_dir = self._memory_dir(workspace_dir)
        entities_dir = memory_dir / "entities"
        if not entities_dir.is_dir():
            return None

        for type_dir in entities_dir.iterdir():
            if not type_dir.is_dir():
                continue
            path = type_dir / f"{entity_id}.md"
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(text)
                return EntityDocument(
                    entity_id=fm.get("entity_id", entity_id),
                    entity_type=fm.get("entity_type", type_dir.name),
                    display_name=fm.get("display_name", ""),
                    status=fm.get("status", "active"),
                    extra_frontmatter={
                        k: v for k, v in fm.items()
                        if k not in {"entity_id", "entity_type", "display_name", "status"}
                    },
                    body=body,
                    source_bulletins=[],  # Parsed from body if needed
                )
        return None

    def list_entities(self, workspace_dir: Path, entity_type: str) -> list[EntityDocument]:
        """List all entities of a given type."""
        memory_dir = self._memory_dir(workspace_dir)
        type_dir = memory_dir / "entities" / entity_type
        if not type_dir.is_dir():
            return []

        results = []
        for md_file in sorted(type_dir.glob("*.md")):
            text = md_file.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
            results.append(EntityDocument(
                entity_id=fm.get("entity_id", md_file.stem),
                entity_type=entity_type,
                display_name=fm.get("display_name", ""),
                status=fm.get("status", "active"),
                extra_frontmatter={
                    k: v for k, v in fm.items()
                    if k not in {"entity_id", "entity_type", "display_name", "status"}
                },
                body=body,
                source_bulletins=[],
            ))
        return results

    # ── Dream Process ─────────────────────────────────────────────

    async def process_bulletin(self, workspace_dir: Path, bulletin: Bulletin) -> dict[str, Any]:
        """Process a single bulletin: extract claims, update entities."""
        memory_dir = self._memory_dir(workspace_dir)

        # Step 1: Extract claims
        from cyborg_server.services.llm_dispatch import LLMDispatchService
        llm = LLMDispatchService(self.ctx)

        claims = await extract_claims_from_bulletin(llm, bulletin)
        wrote_claims = 0
        for claim in claims:
            write_claim(memory_dir, claim)
            wrote_claims += 1

        # Step 2: Update entity documents from claims
        entity_ops = await self._update_entities_from_claims(llm, memory_dir, claims)

        # Step 3: Rebuild indexes
        index_counts = rebuild_indexes(memory_dir)

        return {
            "bulletin_id": bulletin.id,
            "claims_extracted": wrote_claims,
            "entity_ops": entity_ops,
            "indexes": index_counts,
        }

    async def run_dream(self, workspace_dir: Path) -> dict[str, Any]:
        """Process all pending (undigested) bulletins through the dream pipeline."""
        bulletins = self.read_bulletins(workspace_dir, skip_digested=True)
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
                "claims": result["claims_extracted"],
                "entity_ops": result.get("entity_ops", 0),
                "content_preview": (bulletin.content or "")[:120],
            })
            # Mark bulletin as digested
            self._mark_digested(workspace_dir, bulletin)

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
        self, llm: Any, memory_dir: Path, claims: list[Claim]
    ) -> int:
        """Use LLM to update entity documents from claims — one call per entity.

        Before grouping, claims with non-canonical contact subject_ids are
        reconciled against the contacts DB so that contact-blair-nicol,
        unresolved-contact-blair, etc. all merge into the canonical
        contact-{hex8} entity when a DB match exists.
        """
        if not claims:
            return 0

        from collections import defaultdict
        from cyborg_server.services.memory.contact_directory import ContactDirectory
        from cyborg_server.services.memory.reconcile import reconcile_contact_id

        directory = None
        if self.ctx and hasattr(self.ctx, "db") and self.ctx.db:
            directory = await ContactDirectory.load(self.ctx.db)
        self._contact_dir_cache = directory

        # Pre-compute display_name lookup from existing entity files so we can
        # reconcile even when the claim doesn't carry a display_name itself.
        existing_name_map = self._index_contact_display_names(memory_dir)

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

        # Pre-compute shared context
        all_existing_ids = self._list_all_entity_ids(memory_dir)
        contact_ids = {eid for eid in claims_by_entity if eid.startswith("contact-")}
        contact_name_map = await self._lookup_contact_names(contact_ids) if contact_ids else {}

        wrote = 0
        for entity_id, entity_claims in claims_by_entity.items():
            wrote += await self._update_single_entity(
                llm, memory_dir, entity_id, entity_claims,
                all_existing_ids=all_existing_ids,
                contact_name_map=contact_name_map,
            )

        return wrote

    def _index_contact_display_names(self, memory_dir: Path) -> dict[str, str]:
        """Return {entity_id: display_name} for every contact entity on disk."""
        contact_dir = memory_dir / "entities" / "contact"
        if not contact_dir.is_dir():
            return {}
        out: dict[str, str] = {}
        for md_file in contact_dir.glob("*.md"):
            try:
                fm, _ = parse_frontmatter(md_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if fm.get("display_name"):
                out[md_file.stem] = fm["display_name"]
        return out

    async def _update_single_entity(
        self,
        llm: Any,
        memory_dir: Path,
        entity_id: str,
        claims: list[Claim],
        *,
        all_existing_ids: set[str],
        contact_name_map: dict[str, str],
    ) -> int:
        """Update a single entity document from its claims."""
        # Known IDs hint
        known_ids_hint = ""
        if entity_id in all_existing_ids:
            known_ids_hint = (
                "\n\n## KNOWN ENTITY IDS (use these exactly, do not create new IDs for these)\n\n"
                f"- {entity_id}"
            )

        # Build claims summary with source bulletin IDs
        claims_lines = []
        source_bids: set[str] = set()
        for c in claims:
            obj = c.object_id if isinstance(c.object_id, str) else (str(c.object_id) if c.object_id else "")
            bids = ", ".join(c.source_bulletins) if c.source_bulletins else ""
            claims_lines.append(f"- [{c.type}] {c.subject_id} {c.predicate} {obj}  (from: {bids})")
            source_bids.update(c.source_bulletins or [])

        # Build existing entity doc context
        existing_lines = []
        entity_raw = self._read_entity_raw(memory_dir, entity_id)
        if entity_raw:
            existing_lines.append(f"[{entity_id}]\n{entity_raw[:500]}")

        user_prompt = "## NEW CLAIMS\n\n" + "\n".join(claims_lines)
        if existing_lines:
            user_prompt += "\n\n## EXISTING ENTITIES\n\n" + "\n\n".join(existing_lines)
        user_prompt += known_ids_hint

        # Source bulletin IDs
        if source_bids:
            user_prompt += "\n\n## SOURCE BULLETIN IDS\n\n"
            user_prompt += "Use these exact IDs in Source Bulletins sections. Do not invent bulletin IDs:\n"
            for bid in sorted(source_bids):
                user_prompt += f"- {bid}\n"

        # Contact name map
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
            return 0

        if not isinstance(operations, list):
            return 0

        wrote = 0
        for op in operations:
            if not isinstance(op, dict):
                continue
            action = op.get("action")
            if action in ("write_entity", "create", "update"):
                content = op.get("content", "")
                if content:
                    try:
                        fm, body = parse_frontmatter(content)
                    except Exception:
                        logger.warning("Entity update: failed to parse frontmatter from LLM output")
                        fm = {}
                    if fm:
                        eid = fm.get("entity_id", op.get("entity_id", ""))
                        etype = fm.get("entity_type", op.get("entity_type", ""))
                        if eid and etype:
                            normalized = normalize_entity_id(eid, etype)
                            if normalized != eid:
                                fm["entity_id"] = normalized
                                content = serialize_frontmatter(fm, body)
                                old_path = memory_dir / "entities" / etype / f"{eid}.md"
                                if old_path.is_file():
                                    old_path.unlink()
                            eid = normalized
                            # If this is a contact and we have a canonical
                            # entity_id from reconciliation that differs,
                            # override the LLM's choice to avoid creating
                            # name-slug / unresolved- duplicates.
                            if (
                                etype == "contact"
                                and entity_id
                                and entity_id != eid
                                and entity_id.startswith("contact-")
                            ):
                                old_path = memory_dir / "entities" / etype / f"{eid}.md"
                                fm["entity_id"] = entity_id
                                content = serialize_frontmatter(fm, body)
                                if old_path.is_file():
                                    old_path.unlink()
                                eid = entity_id
                            # Enrich with contact_id/email/phone if canonical
                            eid = self._enrich_contact_frontmatter_inplace(
                                memory_dir, etype, eid, fm, body
                            )
                            entities_dir = memory_dir / "entities" / etype
                            entities_dir.mkdir(parents=True, exist_ok=True)
                            path = entities_dir / f"{eid}.md"
                            path.write_text(content, encoding="utf-8")
                            wrote += 1
                    else:
                        entity = EntityDocument(
                            entity_id=op.get("entity_id", ""),
                            entity_type=op.get("entity_type", ""),
                            display_name=op.get("display_name", ""),
                            status=op.get("status", "active"),
                            extra_frontmatter=op.get("extra_frontmatter", {}),
                            body=op.get("body", content),
                            related_entities=op.get("related_entities", {}),
                            source_bulletins=op.get("source_bulletins", []),
                        )
                        if entity.entity_id and entity.entity_type:
                            entity.entity_id = normalize_entity_id(entity.entity_id, entity.entity_type)
                            entities_dir = memory_dir / "entities" / entity.entity_type
                            entities_dir.mkdir(parents=True, exist_ok=True)
                            path = entities_dir / f"{entity.entity_id}.md"
                            fm_out = {
                                "entity_id": entity.entity_id,
                                "entity_type": entity.entity_type,
                                "display_name": entity.display_name,
                                "status": entity.status,
                                **entity.extra_frontmatter,
                            }
                            path.write_text(serialize_frontmatter(fm_out, entity.body), encoding="utf-8")
                            wrote += 1

        return wrote

    def _read_entity_raw(self, memory_dir: Path, entity_id: str) -> str | None:
        """Read raw entity file text by scanning entity directories.

        Tries the exact ID first, then the normalized form.
        """
        entities_dir = memory_dir / "entities"
        if not entities_dir.is_dir():
            return None
        for candidate in (entity_id, normalize_entity_id(entity_id)):
            for type_dir in entities_dir.iterdir():
                if not type_dir.is_dir():
                    continue
                path = type_dir / f"{candidate}.md"
                if path.is_file():
                    return path.read_text(encoding="utf-8")
        return None

    def _list_all_entity_ids(self, memory_dir: Path) -> set[str]:
        """List all entity IDs across all entity type directories."""
        entities_dir = memory_dir / "entities"
        if not entities_dir.is_dir():
            return set()
        result = set()
        for type_dir in entities_dir.iterdir():
            if not type_dir.is_dir():
                continue
            for md_file in type_dir.glob("*.md"):
                result.add(md_file.stem)
        return result

    def _enrich_contact_frontmatter_inplace(
        self,
        memory_dir: Path,
        etype: str,
        eid: str,
        fm: dict,
        body: str,
    ) -> str:
        """If etype is contact and eid is canonical contact-{hex8} and we have
        a cached ContactDirectory, add contact_id/email/phone_number to fm.

        Returns the (possibly updated) eid. Mutates *fm* in place. The caller
        is responsible for serializing fm+body and writing to disk.
        """
        if etype != "contact" or not eid.startswith("contact-"):
            return eid
        import re as _re
        if not _re.match(r"^contact-[a-f0-9]{8}$", eid):
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
        if not contact_ids or not self.ctx or not hasattr(self.ctx, 'db') or not self.ctx.db:
            return {}
        try:
            result = {}
            for cid in contact_ids:
                # Extract hex8 from contact-{hex8}
                hex8 = cid.removeprefix("contact-")[:8]
                row = await self.ctx.db.fetch_one(
                    "SELECT name FROM contacts WHERE id LIKE ? LIMIT 1",
                    (f"{hex8}%",),
                )
                if row and row["name"]:
                    result[cid] = row["name"]
            return result
        except Exception:
            return {}

    # ── Retrieval ─────────────────────────────────────────────────

    async def search_entries(
        self, workspace_dir: Path, query: str, entity_type: str = ""
    ) -> dict[str, Any]:
        """Search memory using entity documents and graph traversal."""
        memory_dir = self._memory_dir(workspace_dir)

        # Collect entity documents
        all_entries: list[dict[str, str]] = []
        entities_dir = memory_dir / "entities"
        if entities_dir.is_dir():
            type_dirs = [entities_dir / entity_type] if entity_type else [
                d for d in entities_dir.iterdir() if d.is_dir()
            ]
            for type_dir in type_dirs:
                if not type_dir.is_dir():
                    continue
                for md_file in type_dir.glob("*.md"):
                    text = md_file.read_text(encoding="utf-8")
                    fm, body = parse_frontmatter(text)
                    all_entries.append({
                        "entity_id": fm.get("entity_id", md_file.stem),
                        "entity_type": fm.get("entity_type", type_dir.name),
                        "display_name": fm.get("display_name", ""),
                        "body": body[:500],
                        "path": str(md_file.relative_to(workspace_dir)),
                    })

        if not all_entries:
            return {"abstract": "No memory entries found.", "results": []}

        # Build catalog for LLM
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
                    "path": entry["path"],
                    "relevance": item.get("relevance", "") if isinstance(item, dict) else "",
                })

        return {"abstract": parsed.get("abstract", ""), "results": results}

    def build_memory_index(self, workspace_dir: Path) -> str:
        """Build compact memory index for system prompt injection."""
        return build_memory_index_text(self._memory_dir(workspace_dir))

    # ── Reflection ────────────────────────────────────────────────

    # ── Person/Contact helpers ────────────────────────────────────

    def ensure_person_entry(
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
        existing = self.read_entity(workspace_dir, canonical_id)
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
        self.write_entity(workspace_dir, entity)
        return canonical_id

    def find_person_entry(
        self,
        workspace_dir: Path,
        *,
        contact_id: str = "",
        name: str = "",
    ) -> str | None:
        """Find a contact entity by ID. Returns full content or None."""
        if contact_id:
            canonical_id = canonical_contact_id(contact_id)
            entity = self.read_entity(workspace_dir, canonical_id)
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
        memory_dir = self._memory_dir(workspace_dir)

        if all:
            # Clear derived data
            for derived in ["claims", "indexes", "aliases"]:
                derived_dir = memory_dir / derived
                if derived_dir.is_dir():
                    for f in derived_dir.glob("*"):
                        if f.is_file():
                            f.unlink()

            # Re-process all bulletins
            bulletins = self.read_bulletins(workspace_dir, limit=10000)
            total_claims = 0
            for bulletin in bulletins:
                result = await self.process_bulletin(workspace_dir, bulletin)
                total_claims += result["claims_extracted"]

            return {"status": "completed", "bulletins_processed": len(bulletins), "claims": total_claims}

        if entity_id:
            # Rebuild indexes only
            counts = rebuild_indexes(memory_dir)
            return {"status": "completed", "indexes": counts}

        return {"status": "no_op"}

    # ── Validation ────────────────────────────────────────────────

    def validate(self, workspace_dir: Path) -> dict[str, Any]:
        """Validate memory structure: check frontmatter, dangling refs."""
        memory_dir = self._memory_dir(workspace_dir)
        issues: list[str] = []

        # Check all entity docs have required fields
        entities_dir = memory_dir / "entities"
        if entities_dir.is_dir():
            for type_dir in entities_dir.iterdir():
                if not type_dir.is_dir():
                    continue
                for md_file in type_dir.glob("*.md"):
                    text = md_file.read_text(encoding="utf-8")
                    fm, _ = parse_frontmatter(text)
                    for field in ["entity_id", "entity_type", "display_name"]:
                        if not fm.get(field):
                            issues.append(f"{md_file.name}: missing {field}")

        return {"valid": len(issues) == 0, "issues": issues}

    # ── Legacy compatibility ──────────────────────────────────────

    def browse_category(self, workspace_dir: Path, wiki: str, category: str) -> list[dict[str, Any]]:
        """Legacy: browse entities by type (category maps to entity_type)."""
        return [
            {
                "slug": e.entity_id,
                "title": e.display_name,
                "modified": 0,
            }
            for e in self.list_entities(workspace_dir, category)
        ]

    def read_entry(self, workspace_dir: Path, wiki: str, category: str, slug: str) -> str | None:
        """Legacy: read an entity by wiki/category/slug."""
        entity = self.read_entity(workspace_dir, slug)
        if entity:
            return serialize_frontmatter(
                {"entity_id": entity.entity_id, "entity_type": entity.entity_type,
                 "display_name": entity.display_name, **entity.extra_frontmatter},
                entity.body,
            )
        return None

    def write_entry(self, workspace_dir: Path, wiki: str, category: str, slug: str, title: str, content: str) -> str:
        """Legacy: write an entity. Category maps to entity_type."""
        entity = EntityDocument(
            entity_id=slug,
            entity_type=category,
            display_name=title,
            body=content,
        )
        return self.write_entity(workspace_dir, entity)

    def list_recent_entries(self, workspace_dir: Path, wiki_names: list[str], limit: int = 50) -> dict[str, Any]:
        """Legacy: list recent entity documents."""
        memory_dir = self._memory_dir(workspace_dir)
        entities_dir = memory_dir / "entities"
        all_entries: list[dict] = []

        if entities_dir.is_dir():
            for type_dir in entities_dir.iterdir():
                if not type_dir.is_dir():
                    continue
                for md_file in type_dir.glob("*.md"):
                    text = md_file.read_text(encoding="utf-8")
                    fm, body = parse_frontmatter(text)
                    summary = ""
                    for line in body.splitlines():
                        if line.strip() and not line.strip().startswith("#"):
                            summary = line.strip()[:80]
                            break
                    all_entries.append({
                        "path": str(md_file.relative_to(workspace_dir)),
                        "wiki": "core",
                        "category": type_dir.name,
                        "slug": md_file.stem,
                        "title": fm.get("display_name", ""),
                        "summary": summary,
                        "modified": md_file.stat().st_mtime,
                    })

        all_entries.sort(key=lambda e: e["modified"], reverse=True)
        return {
            "stats": {"total_entries": len(all_entries)},
            "recent": all_entries[:limit],
        }

    @staticmethod
    def _build_memory_index_static(workspace_dir: Path, wiki_names: list[str]) -> str:
        """Build a compact memory index without a service instance."""
        return build_memory_index_text(workspace_dir.expanduser() / "memory")

    def rebuild_wiki_index(self, workspace_dir: Path, wiki_name: str) -> None:
        """Legacy: no-op, indexes are rebuilt as needed."""
        pass

    # ── Config compatibility ──────────────────────────────────────

    async def resolve_accessible_wikis(self, workspace_dir: Path, session_key: str | None = None) -> list[str]:
        """Legacy: returns ['core']."""
        return ["core"]

    async def resolve_writable_wikis(self, workspace_dir: Path, session_key: str | None = None) -> list[str]:
        """Legacy: returns ['core']."""
        return ["core"]

    def validate_wiki_category(self, workspace_dir: Path, wiki: str, category: str) -> bool:
        """Legacy: always True."""
        return True

    def move_to_digested(self, workspace_dir: Path, bulletin_paths: list[Path]) -> None:
        """Legacy: no-op, bulletins are immutable."""
        pass

    def _mark_digested(self, workspace_dir: Path, bulletin: Bulletin) -> None:
        """Add digested: true to a bulletin's frontmatter."""
        memory_dir = self._memory_dir(workspace_dir)
        bulletin_dir = memory_dir / "bulletins"
        for md_file in bulletin_dir.rglob("*.md"):
            if bulletin.id in md_file.name:
                raw = md_file.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(raw)
                if fm and not fm.get("digested"):
                    fm["digested"] = True
                    new_raw = serialize_frontmatter(fm, body)
                    md_file.write_text(new_raw, encoding="utf-8")
                return
