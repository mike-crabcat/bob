"""Persistent memory wiki — markdown files with auto-generated indexes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from bob_server.services.base import BaseService, utcnow

logger = logging.getLogger(__name__)

_DEFAULT_ACCESS_YML = """\
wikis:
  core:
    description: "General knowledge"
    categories: [people, facts, events, locations, research, bulletins, digested]
    access: always
    write: always
"""

_config_cache: tuple[float, dict[str, Any]] | None = None  # (mtime, parsed config)


class _ClassProperty:
    """Descriptor that works like @property but at the class level."""
    def __init__(self, fn):
        self.fn = fn

    def __get__(self, obj, objtype=None):
        return self.fn(objtype or type(obj))


classproperty = _ClassProperty


def _parse_entry_summary(text: str) -> tuple[str, str]:
    """Extract (title, one-line summary) from a markdown entry."""
    lines = text.strip().splitlines()
    title = ""
    summary = ""

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            continue
        if not stripped or stripped.startswith("#"):
            if title:
                continue
            continue
        if title and not summary:
            summary = stripped
            break

    return title or "untitled", summary


class MemoryService(BaseService):
    """Reads and writes memory wiki entries as markdown files."""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)

    @staticmethod
    def _memory_dir(workspace_dir: Path) -> Path:
        return workspace_dir.expanduser() / "memory"

    # ── Config ──────────────────────────────────────────────────

    @staticmethod
    def load_access_config(workspace_dir: Path) -> dict[str, Any]:
        """Parse memory/access.yml with mtime-based caching."""
        global _config_cache

        config_path = MemoryService._memory_dir(workspace_dir) / "access.yml"
        if not config_path.is_file():
            return {"wikis": {}}

        mtime = config_path.stat().st_mtime
        if _config_cache is not None and _config_cache[0] == mtime:
            return _config_cache[1]

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        wikis = raw.get("wikis", {}) if isinstance(raw, dict) else {}
        _config_cache = (mtime, {"wikis": wikis})
        return _config_cache[1]

    async def resolve_accessible_wikis(
        self, workspace_dir: Path, session_key: str | None = None
    ) -> list[str]:
        """Return wiki names accessible for the given session."""
        config = self.load_access_config(workspace_dir)
        result: list[str] = []

        for name, wiki_conf in config.get("wikis", {}).items():
            access = wiki_conf.get("access", "always")
            if access == "always":
                result.append(name)
            elif access == "trusted" and session_key:
                row = await self.db.fetch_one(
                    "SELECT 1 AS ok FROM session_participants "
                    "WHERE session_key = ? AND is_trusted = 1 LIMIT 1",
                    (session_key,),
                )
                if row:
                    result.append(name)
        return result

    async def resolve_writable_wikis(
        self, workspace_dir: Path, session_key: str | None = None
    ) -> list[str]:
        """Return wiki names writable for the given session."""
        config = self.load_access_config(workspace_dir)
        result: list[str] = []

        for name, wiki_conf in config.get("wikis", {}).items():
            write = wiki_conf.get("write", "always")
            if write == "always":
                result.append(name)
            elif write == "trusted" and session_key:
                row = await self.db.fetch_one(
                    "SELECT 1 AS ok FROM session_participants "
                    "WHERE session_key = ? AND is_trusted = 1 LIMIT 1",
                    (session_key,),
                )
                if row:
                    result.append(name)
        return result

    def validate_wiki_category(
        self, workspace_dir: Path, wiki: str, category: str
    ) -> bool:
        """Check that wiki and category are defined in config."""
        config = self.load_access_config(workspace_dir)
        wiki_conf = config.get("wikis", {}).get(wiki)
        if not wiki_conf:
            return False
        return category in wiki_conf.get("categories", [])

    # ── Index ───────────────────────────────────────────────────

    @staticmethod
    def _build_memory_index_static(workspace_dir: Path, wiki_names: list[str]) -> str:
        """Build a compact memory index without requiring a service instance."""
        memory_dir = workspace_dir.expanduser() / "memory"
        parts: list[str] = []

        for wiki_name in wiki_names:
            index_path = memory_dir / wiki_name / "_index.md"
            if index_path.is_file():
                content = index_path.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(content)

        if not parts:
            return ""

        header = (
            "You have persistent memory. Use memory_read for full details, "
            "memory_search to find entries, memory_write to update.\n"
        )
        return header + "\n\n".join(parts)

    def build_memory_index(
        self, workspace_dir: Path, wiki_names: list[str]
    ) -> str:
        """Build a compact memory index from accessible wikis."""
        return self._build_memory_index_static(workspace_dir, wiki_names)

    def rebuild_wiki_index(self, workspace_dir: Path, wiki_name: str) -> None:
        """Scan a wiki's files and regenerate its _index.md."""
        config = self.load_access_config(workspace_dir)
        wiki_conf = config.get("wikis", {}).get(wiki_name)
        if not wiki_conf:
            return

        memory_dir = self._memory_dir(workspace_dir)
        wiki_dir = memory_dir / wiki_name
        categories = wiki_conf.get("categories", [])

        lines: list[str] = [f"### {wiki_name}"]

        for cat in categories:
            cat_dir = wiki_dir / cat
            if not cat_dir.is_dir():
                continue
            entries: list[str] = []
            for md_file in sorted(cat_dir.rglob("*.md")):
                if md_file.name.startswith("_"):
                    continue
                slug = md_file.stem
                text = md_file.read_text(encoding="utf-8")
                title, summary = _parse_entry_summary(text)
                entry_str = f"{slug} ({title}"
                if summary:
                    entry_str += f", {summary[:80]}"
                entry_str += ")"
                entries.append(entry_str)
            if entries:
                lines.append(f"**{cat}**: " + ", ".join(entries))

        index_content = "\n".join(lines)
        index_path = wiki_dir / "_index.md"
        index_path.write_text(index_content, encoding="utf-8")
        logger.info("Rebuilt memory index for wiki '%s': %d chars", wiki_name, len(index_content))

    # ── CRUD ────────────────────────────────────────────────────

    def write_entry(
        self,
        workspace_dir: Path,
        wiki: str,
        category: str,
        slug: str,
        title: str,
        content: str,
    ) -> str:
        """Write a memory entry file and rebuild the wiki index."""
        memory_dir = self._memory_dir(workspace_dir)
        entry_path = memory_dir / wiki / category / f"{slug}.md"

        entry_path.parent.mkdir(parents=True, exist_ok=True)
        file_content = f"# {title}\n\n{content}\n"
        entry_path.write_text(file_content, encoding="utf-8")

        self.rebuild_wiki_index(workspace_dir, wiki)
        logger.info("Memory write: %s/%s/%s", wiki, category, slug)
        rel = f"memory/{wiki}/{category}/{slug}.md"
        return rel

    def read_entry(
        self, workspace_dir: Path, wiki: str, category: str, slug: str
    ) -> str | None:
        """Read a memory entry, returning None if not found."""
        entry_path = self._memory_dir(workspace_dir) / wiki / category / f"{slug}.md"
        if not entry_path.is_file():
            return None
        return entry_path.read_text(encoding="utf-8")

    async def search_entries(
        self, workspace_dir: Path, wiki_names: list[str], query: str
    ) -> dict[str, Any]:
        """Semantic search across memory entries using an LLM.

        Returns {"abstract": str, "results": [{path, title, relevance}]}.
        """
        memory_dir = self._memory_dir(workspace_dir)

        # Collect all entries with full text
        all_entries: list[dict[str, str]] = []
        for wiki_name in wiki_names:
            wiki_dir = memory_dir / wiki_name
            if not wiki_dir.is_dir():
                continue
            for md_file in wiki_dir.rglob("*.md"):
                if md_file.name.startswith("_"):
                    continue
                text = md_file.read_text(encoding="utf-8")
                rel = md_file.relative_to(memory_dir)
                parts = rel.parts
                category = parts[1] if len(parts) > 2 else ""
                slug = md_file.stem
                title, summary = _parse_entry_summary(text)
                workspace_path = f"memory/{wiki_name}/{category}/{slug}.md"
                all_entries.append({
                    "wiki": wiki_name,
                    "category": category,
                    "slug": slug,
                    "title": title,
                    "summary": summary,
                    "full_text": text,
                    "workspace_path": workspace_path,
                })

        if not all_entries:
            return {"abstract": "No memory entries found.", "results": []}

        # Build catalog with workspace paths and full content
        catalog_lines: list[str] = []
        for i, entry in enumerate(all_entries):
            catalog_lines.append(
                f"[{i}] {entry['workspace_path']}\n"
                f"    Title: {entry['title']}\n"
                f"    Summary: {entry['summary']}\n"
                f"    Content: {entry['full_text'][:500]}"
            )
        catalog = "\n\n".join(catalog_lines)

        system_prompt = (
            "You are a memory search agent. Given a query and a catalog of memory entries, "
            "find entries relevant to the query by meaning (not just keywords).\n\n"
            "Return a JSON object with exactly these keys:\n"
            '- "abstract": 1-2 sentence summary of what you found relevant to the query\n'
            '- "results": array of objects, each with:\n'
            '    "index": integer (matching the [N] in the catalog)\n'
            '    "relevance": one sentence explaining WHY this entry is relevant to the query\n'
            "\nReturn ONLY valid JSON. Return {\"abstract\": \"No matches found.\", \"results\": []} if nothing matches."
        )
        user_prompt = f"Query: {query}\n\nCatalog:\n{catalog}"

        from bob_server.services.llm_dispatch import LLMDispatchService

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
            abstract = parsed.get("abstract", "")
            raw_results = parsed.get("results", [])
        except (json.JSONDecodeError, ValueError):
            # Fallback to keyword match
            abstract = "Keyword fallback results."
            raw_results = [
                {"index": i, "relevance": "Keyword match"}
                for i, e in enumerate(all_entries)
                if query.lower() in (e["title"] + " " + e["summary"] + " " + e["full_text"]).lower()
            ]

        results: list[dict[str, str]] = []
        for item in raw_results:
            idx = item.get("index") if isinstance(item, dict) else item
            if not isinstance(idx, int) or idx < 0 or idx >= len(all_entries):
                continue
            entry = all_entries[idx]
            results.append({
                "path": entry["workspace_path"],
                "title": entry["title"],
                "relevance": item.get("relevance", "") if isinstance(item, dict) else "",
            })

        return {"abstract": abstract, "results": results}

    def browse_category(
        self, workspace_dir: Path, wiki: str, category: str
    ) -> list[dict[str, Any]]:
        """List entries in a wiki category."""
        cat_dir = self._memory_dir(workspace_dir) / wiki / category
        if not cat_dir.is_dir():
            return []

        entries: list[dict[str, Any]] = []
        for md_file in sorted(cat_dir.rglob("*.md")):
            if md_file.name.startswith("_"):
                continue
            slug = md_file.stem
            text = md_file.read_text(encoding="utf-8")
            title, _ = _parse_entry_summary(text)
            entries.append({
                "slug": slug,
                "title": title,
                "modified": md_file.stat().st_mtime,
            })
        return entries

    def list_recent_entries(
        self, workspace_dir: Path, wiki_names: list[str], limit: int = 50
    ) -> dict[str, Any]:
        """Return recent memory entries and aggregate stats."""
        memory_dir = self._memory_dir(workspace_dir)
        all_entries: list[dict[str, Any]] = []
        stats: dict[str, Any] = {"total_entries": 0, "wikis": {}}

        for wiki_name in wiki_names:
            wiki_dir = memory_dir / wiki_name
            if not wiki_dir.is_dir():
                continue
            wiki_stats: dict[str, Any] = {"entries": 0, "categories": {}}
            for md_file in wiki_dir.rglob("*.md"):
                if md_file.name.startswith("_"):
                    continue
                rel = md_file.relative_to(memory_dir)
                parts = rel.parts
                category = parts[1] if len(parts) > 2 else ""
                slug = md_file.stem
                text = md_file.read_text(encoding="utf-8")
                title, summary = _parse_entry_summary(text)
                mtime = md_file.stat().st_mtime

                wiki_stats["entries"] += 1
                wiki_stats["categories"][category] = wiki_stats["categories"].get(category, 0) + 1

                all_entries.append({
                    "path": f"memory/{wiki_name}/{category}/{slug}.md",
                    "wiki": wiki_name,
                    "category": category,
                    "slug": slug,
                    "title": title,
                    "summary": summary,
                    "modified": mtime,
                })

            if wiki_stats["entries"]:
                stats["wikis"][wiki_name] = wiki_stats

        stats["total_entries"] = len(all_entries)
        all_entries.sort(key=lambda e: e["modified"], reverse=True)
        return {"stats": stats, "recent": all_entries[:limit]}

    # ── Bulletins ───────────────────────────────────────────────

    async def write_bulletin(
        self,
        workspace_dir: Path,
        *,
        session_key: str,
        source_type: str,
        time_window_from: str = "",
        time_window_to: str = "",
        participants: list[str] | None = None,
        contact_ids: list[str] | None = None,
        content: str = "",
        intended_category: str = "",
        intended_slug: str = "",
        intended_title: str = "",
    ) -> str:
        """Write a raw bulletin to the bulletins category."""
        from uuid import uuid4

        slug = f"blt-{uuid4().hex[:12]}"
        title = f"Bulletin: {content[:60]}"

        meta_lines = [
            f"- source_session: {session_key}",
            f"- source_type: {source_type}",
        ]
        if time_window_from and time_window_to:
            meta_lines.append(f"- time_window: {time_window_from} to {time_window_to}")
        if participants:
            meta_lines.append(f"- participants: {', '.join(participants)}")
        if contact_ids:
            meta_lines.append(f"- contact_ids: {', '.join(contact_ids)}")
        if intended_category:
            meta_lines.append(f"- intended_category: {intended_category}")
            meta_lines.append(f"- intended_slug: {intended_slug}")
            meta_lines.append(f"- intended_title: {intended_title}")

        meta_lines.append(f"- created_at: {utcnow().isoformat()}")

        body = "\n".join(meta_lines) + "\n\n" + content
        return self.write_entry(workspace_dir, "core", "bulletins", slug, title, body)

    def read_bulletins(self, workspace_dir: Path) -> list[dict[str, Any]]:
        """Read all pending bulletins from the bulletins category."""
        bulletins_dir = self._memory_dir(workspace_dir) / "core" / "bulletins"
        if not bulletins_dir.is_dir():
            return []

        bulletins: list[dict[str, Any]] = []
        for md_file in sorted(bulletins_dir.glob("*.md")):
            if md_file.name.startswith("_"):
                continue
            text = md_file.read_text(encoding="utf-8")
            lines = text.strip().splitlines()

            meta: dict[str, str] = {}
            content_lines: list[str] = []
            in_content = False
            has_meta = False
            for line in lines:
                if in_content:
                    content_lines.append(line)
                elif line.startswith("# "):
                    continue
                elif not line.strip():
                    if has_meta:
                        in_content = True
                elif line.startswith("- "):
                    key, _, val = line[2:].partition(": ")
                    meta[key.strip()] = val.strip()
                    has_meta = True
                else:
                    in_content = True
                    content_lines.append(line)

            bulletins.append({
                "path": md_file,
                "slug": md_file.stem,
                "source_session": meta.get("source_session", ""),
                "source_type": meta.get("source_type", ""),
                "time_window": meta.get("time_window", ""),
                "participants": meta.get("participants", ""),
                "contact_ids": meta.get("contact_ids", ""),
                "intended_category": meta.get("intended_category", ""),
                "intended_slug": meta.get("intended_slug", ""),
                "intended_title": meta.get("intended_title", ""),
                "content": "\n".join(content_lines).strip(),
                "created_at": md_file.stat().st_mtime,
            })
        return bulletins

    def move_to_digested(self, workspace_dir: Path, bulletin_paths: list[Path]) -> None:
        """Move processed bulletins to the digested category."""
        if not bulletin_paths:
            return

        digested_dir = self._memory_dir(workspace_dir) / "core" / "digested"
        digested_dir.mkdir(parents=True, exist_ok=True)

        for path in bulletin_paths:
            dest = digested_dir / path.name
            path.rename(dest)
            logger.info("Digested bulletin: %s", path.name)

        self.rebuild_wiki_index(workspace_dir, "core")

    # ── Reflection ──────────────────────────────────────────────

    async def reflect_and_update(
        self,
        workspace_dir: Path,
        session_key: str,
        summary_text: str,
        memory_prompts: list[str],
        *,
        active_from: str = "",
        active_to: str = "",
        participants: list[str] | None = None,
        contact_ids: list[str] | None = None,
    ) -> None:
        """Write a bulletin from conversation summary for the dream process to curate."""
        if not memory_prompts:
            return

        content = "\n".join(f"- {p}" for p in memory_prompts)
        await self.write_bulletin(
            workspace_dir,
            session_key=session_key,
            source_type="reflect",
            time_window_from=active_from,
            time_window_to=active_to,
            participants=participants or [],
            contact_ids=contact_ids or [],
            content=content,
        )

    # ── Dream ───────────────────────────────────────────────────

    _CURATED_CATEGORIES = ("people", "facts", "events", "locations", "research")

    _CATEGORY_TEMPLATES: dict[str, dict[str, str]] = {
        "people": {
            "Overview": "One-line identity summary",
            "Personality": "Character traits, communication style, humor",
            "Interests": "Hobbies, passions, media preferences",
            "Dietary": "Food preferences, restrictions, allergies",
            "Work": "Job, company, role, work style preferences",
            "Family": "Family members, relationships, household details",
            "Preferences": "Communication preferences, scheduling, general likes/dislikes",
            "Contact": "Phone, email, address, preferred channels",
            "Relationships": "Key relationships to other contacts",
        },
        "events": {
            "Summary": "What happened in one sentence",
            "Date": "When it happened",
            "Participants": "Who was involved",
            "Location": "Where it took place",
            "Details": "Key facts and outcomes",
            "Follow-up": "Pending actions or consequences",
        },
        "facts": {
            "Summary": "The fact in one sentence",
            "Details": "Supporting details and context",
            "Procedures": "Step-by-step instructions if applicable",
        },
        "locations": {
            "Description": "What this place is",
            "Address": "Physical or virtual address",
            "Notes": "Access details, tips, associations",
            "Related": "Connected people or events",
        },
        "research": {
            "Topic": "What was investigated",
            "Findings": "Key discoveries or results",
            "Status": "Current state (open, resolved, abandoned)",
            "Notes": "Additional context and links",
        },
    }

    @classmethod
    def _template_headers(cls, category: str) -> list[str]:
        tmpl = cls._CATEGORY_TEMPLATES.get(category, {})
        return list(tmpl.keys())

    _DREAM_SYSTEM_BASE = (
        "You are a memory curation agent. Review new bulletins and reconcile with existing curated entries.\n"
        "\n"
        "Your job is to extract factual claims from bulletins and write them to memory. You should be liberal\n"
        "about extracting information — even if a bulletin contains hedging language like 'unverified' or\n"
        "'test data', extract the factual claims it contains. You are NOT responsible for verifying claims;\n"
        "you are responsible for recording them.\n"
        "\n"
        "For each piece of new information:\n"
        "- CREATE a new entry in the correct category, OR\n"
        "- UPDATE an existing entry (merge new info, resolve conflicts — newer info wins), OR\n"
        "- IGNORE only if the bulletin contains no factual claims at all\n"
        "\n"
        "## People-First Processing\n"
        "\n"
        "Person profiles are the MOST IMPORTANT category. When bulletins contain information about a person\n"
        "(identified by name or {{{{contact:ID|Name}}}} references):\n"
        "\n"
        "1. ALWAYS create or update a people/ entry for that person — never store person info only in facts/ or research/.\n"
        "2. If no people/ entry exists for the person, create one using the people template. Use the contact name as slug.\n"
        "3. Merge new information into the correct section headers (Personality, Interests, Dietary, Work, Family, Preferences, etc.).\n"
        "4. If a bulletin references multiple people, create/update entries for EACH person separately.\n"
        "5. Embed the contact_id in a metadata comment at the top: <!-- contact_id: ID -->\n"
        "6. Person profiles should be comprehensive and current — this is your top priority.\n"
        "\n"
        "{templates_section}"
        "\n"
        "Rules for headers:\n"
        "- Use ## (h2) headings for every section — never bare text or bold\n"
        "- Include only sections where information exists — omit empty sections\n"
        "- Place sections in the order shown in the template\n"
        "- Content under each header should be concise bullet points\n"
        "\n"
        "## Transcript References\n"
        "\n"
        "When a bulletin has session metadata, include an inline transcript tag on the relevant bullet point:\n"
        "`[[session:{{session_key}} {{window}}]]`\n"
        "\n"
        "The session_key and window come from the bulletin metadata. Place the tag at the end of the relevant bullet point.\n"
        "Include transcript references whenever possible, but do not require them — some bulletins come from imported data.\n"
        "\n"
        "Example:\n"
        "- Prefers morning meetings, not available before 10am [[session:whatsapp:contact:abc 2026-05-24T10:00..10:30]]\n"
        "\n"
        "When updating an existing entry, preserve existing [[session:...]] tags and add new ones for new information.\n"
        "\n"
        "## General Rules\n"
        "- One entry per topic/person — merge multiple bulletins about the same thing\n"
        "- Use the same slug when updating an existing entry so it overwrites\n"
        "- Content should be succinct — bullet points, not paragraphs\n"
        "- Include source_bulletins: [indices] in each operation\n"
        "\n"
        'Return a JSON array: [{{"action":"write","wiki":"core","category":"...","slug":"...","title":"...","content":"...","source_bulletins":[1,3]}}]\n'
        "Return an empty array [] if nothing is worth remembering."
    )

    def _build_dream_system_prompt(self, workspace_dir: Path) -> str:
        """Build the dream system prompt, injecting _template.md from each category."""
        templates: list[str] = []
        for cat in self._CURATED_CATEGORIES:
            template_path = workspace_dir / "core" / cat / "_template.md"
            if template_path.exists():
                content = template_path.read_text().strip()
                templates.append(f"**{cat}**:\n{content}")
            else:
                headers = ", ".join(self._template_headers(cat))
                templates.append(f"**{cat}**: {headers}")

        templates_section = "## Category Templates\n\n" + "\n\n".join(templates)
        return self._DREAM_SYSTEM_BASE.format(templates_section=templates_section)

    # Static fallback for evals and tests that access the prompt without a workspace dir.
    @classproperty
    def _DREAM_SYSTEM_PROMPT(cls) -> str:  # type: ignore[no-redef]
        templates: list[str] = []
        for cat in cls._CURATED_CATEGORIES:
            headers = ", ".join(cls._template_headers(cat))
            templates.append(f"**{cat}**: {headers}")
        templates_section = "## Category Templates\n\n" + "\n\n".join(templates)
        return cls._DREAM_SYSTEM_BASE.format(templates_section=templates_section)

    async def run_dream(self, workspace_dir: Path) -> dict[str, Any]:
        """Curate pending bulletins into categorized memory entries via LLM."""
        bulletins = self.read_bulletins(workspace_dir)
        if not bulletins:
            return {"status": "empty", "bulletins_processed": 0, "entries_created": 0,
                    "bulletin_slugs": [], "operations": [], "duration_seconds": 0}

        logger.info("Memory dream: processing %d bulletins", len(bulletins))
        start_time = __import__("time").monotonic()

        # Build bulletin catalog
        bulletin_lines: list[str] = []
        for i, b in enumerate(bulletins, 1):
            header = f"[{i}] {b['slug']}"
            if b["source_session"]:
                header += f" (session: {b['source_session'][:40]}"
                if b["time_window"]:
                    header += f", window: {b['time_window']}"
                header += ")"
            if b["participants"]:
                header += f" participants: {b['participants']}"
            bulletin_lines.append(f"{header}\n{b['content']}")

        # Build existing entries catalog
        existing_lines: list[str] = []
        for cat in self._CURATED_CATEGORIES:
            entries = self.browse_category(workspace_dir, "core", cat)
            for entry in entries:
                existing_lines.append(
                    f"[{cat}/{entry['slug']}] {entry['title']}"
                )

        user_prompt = "## NEW BULLETINS\n\n" + "\n\n".join(bulletin_lines)
        if existing_lines:
            user_prompt += "\n\n## EXISTING CURATED ENTRIES\n\n" + "\n\n".join(existing_lines)

        from bob_server.services.llm_dispatch import LLMDispatchService

        llm = LLMDispatchService(self.ctx)
        response = await llm.chat(
            messages=[
                {"role": "system", "content": self._build_dream_system_prompt(workspace_dir)},
                {"role": "user", "content": user_prompt},
            ],
            model=llm.memory_model,
            call_category="memory_dream",
            temperature=0.4,
            max_tokens=2000,
        )

        raw_response = response
        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            operations = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Memory dream: failed to parse LLM response")
            return {"status": "failed", "bulletins_processed": len(bulletins), "entries_created": 0,
                    "bulletin_slugs": [b["slug"] for b in bulletins], "operations": [],
                    "duration_seconds": __import__("time").monotonic() - start_time,
                    "raw_response": raw_response}

        if not isinstance(operations, list):
            return {"status": "failed", "bulletins_processed": len(bulletins), "entries_created": 0,
                    "bulletin_slugs": [b["slug"] for b in bulletins], "operations": [],
                    "duration_seconds": __import__("time").monotonic() - start_time,
                    "raw_response": raw_response}

        config = self.load_access_config(workspace_dir)
        valid_categories = set()
        for wiki_conf in config.get("wikis", {}).values():
            valid_categories.update(wiki_conf.get("categories", []))

        wrote = 0
        successful_ops: list[dict[str, str]] = []
        for op in operations:
            if op.get("action") != "write":
                continue
            wiki = op.get("wiki", "")
            category = op.get("category", "")
            slug = op.get("slug", "")
            title = op.get("title", "")
            content = op.get("content", "")

            if not all([wiki, category, slug, title, content]):
                continue
            if category in ("bulletins", "digested"):
                continue
            if not self.validate_wiki_category(workspace_dir, wiki, category):
                continue

            self.write_entry(workspace_dir, wiki, category, slug, title, content)
            logger.info("Memory dream wrote: %s/%s/%s", wiki, category, slug)
            wrote += 1
            successful_ops.append({"category": category, "slug": slug, "title": title})

        # Move all processed bulletins to digested
        paths = [b["path"] for b in bulletins]
        self.move_to_digested(workspace_dir, paths)
        duration = __import__("time").monotonic() - start_time
        logger.info("Memory dream complete: %d entries from %d bulletins", wrote, len(bulletins))

        return {
            "status": "completed",
            "bulletins_processed": len(bulletins),
            "entries_created": wrote,
            "bulletin_slugs": [b["slug"] for b in bulletins],
            "operations": successful_ops,
            "duration_seconds": duration,
            "raw_response": raw_response,
        }

    # ── Lint ─────────────────────────────────────────────────────

    _LINT_SYSTEM_PROMPT = (
        "You are a memory formatting agent. Restructure the following memory entry to match "
        "the expected section headers for its category.\n"
        "\n"
        "Category: {category}\n"
        "Expected headers: {headers}\n"
        "\n"
        "Rules:\n"
        "- Preserve ALL existing information — do not remove or change facts\n"
        "- Preserve all [[session:...]] transcript reference tags\n"
        "- Reorganize content under the correct headers\n"
        "- Use concise bullet points under each header\n"
        "- Omit headers that have no content\n"
        "- Place headers in the order listed above\n"
        "- Return ONLY the content (no title header, no markdown fences)"
    )

    async def lint_entries(self, workspace_dir: Path) -> dict[str, Any]:
        """Restructure all curated entries to match category templates."""
        from bob_server.services.llm_dispatch import LLMDispatchService

        llm = LLMDispatchService(self.ctx)
        linted = 0
        by_category: dict[str, int] = {}

        for category in self._CURATED_CATEGORIES:
            tmpl = self._CATEGORY_TEMPLATES.get(category)
            if not tmpl:
                continue
            headers = ", ".join(tmpl.keys())
            entries = self.browse_category(workspace_dir, "core", category)
            cat_count = 0

            for entry in entries:
                full = self.read_entry(workspace_dir, "core", category, entry["slug"])
                if not full:
                    continue

                prompt = self._LINT_SYSTEM_PROMPT.format(
                    category=category, headers=headers,
                )
                try:
                    response = await llm.chat(
                        messages=[
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": full},
                        ],
                        model=llm.memory_model,
                        call_category="memory_lint",
                        temperature=0.2,
                        max_tokens=1000,
                    )
                except Exception:
                    logger.warning("Lint failed for %s/%s", category, entry["slug"])
                    continue

                text = response.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

                if text and text != full:
                    self.write_entry(
                        workspace_dir, "core", category,
                        entry["slug"], entry["title"], text,
                    )
                    cat_count += 1

            if cat_count:
                by_category[category] = cat_count
                linted += cat_count

        return {"linted": linted, "categories": by_category}

    # ── People backfill ──────────────────────────────────────────

    _BACKFILL_SYSTEM_PROMPT = (
        "You are a memory extraction agent. Your job is to scan existing memory entries\n"
        "and extract any information about specific people that should be in person profiles.\n"
        "\n"
        "You will receive:\n"
        "1. EXISTING PEOPLE ENTRIES — already-curated person profiles\n"
        "2. SOURCE ENTRIES — entries from other categories (facts, events, locations, research)\n"
        "\n"
        "For each person mentioned in the source entries:\n"
        "- If a people entry already exists for that person, extract any NEW facts not yet in their profile\n"
        "- If no people entry exists, extract all person-relevant facts to create a new profile\n"
        "\n"
        "People-relevant facts include: personality traits, preferences, dietary info, work details,\n"
        "family info, interests, communication style, personal details, relationships.\n"
        "\n"
        "Use the people category template headers:\n"
        "Overview, Personality, Interests, Dietary, Work, Family, Preferences, Contact, Relationships\n"
        "\n"
        "Rules:\n"
        "- Use ## (h2) headings for sections — omit sections with no content\n"
        "- Content should be concise bullet points\n"
        "- If a person is referenced by {{contact:ID|Name}}, include <!-- contact_id: ID --> at the top\n"
        "- Use the person's display name as the title and derive a slug from it (lowercase, hyphens)\n"
        "- Do NOT duplicate information already in the existing people entries\n"
        "- Include transcript references [[session:...]] if present in the source\n"
        "\n"
        'Return a JSON array of write operations: [{{"action":"write","wiki":"core","category":"people",'
        '"slug":"...","title":"...","content":"...","source":"fact/slug or event/slug etc"}}]\n'
        "Return an empty array [] if no person-relevant information is found in the sources."
    )

    async def backfill_people(self, workspace_dir: Path) -> dict[str, Any]:
        """Scan existing non-people entries and extract person-relevant facts into people profiles."""
        memory_dir = self._memory_dir(workspace_dir)
        source_categories = ("facts", "events", "locations", "research")

        # Collect existing people entries
        existing_people: list[dict[str, str]] = []
        people_entries = self.browse_category(workspace_dir, "core", "people")
        for entry in people_entries:
            full = self.read_entry(workspace_dir, "core", "people", entry["slug"])
            if full:
                existing_people.append({
                    "slug": entry["slug"],
                    "title": entry["title"],
                    "content": full[:800],
                })

        # Collect source entries from other categories
        source_entries: list[dict[str, str]] = []
        for cat in source_categories:
            entries = self.browse_category(workspace_dir, "core", cat)
            for entry in entries:
                full = self.read_entry(workspace_dir, "core", cat, entry["slug"])
                if full:
                    source_entries.append({
                        "category": cat,
                        "slug": entry["slug"],
                        "title": entry["title"],
                        "content": full,
                    })

        if not source_entries:
            return {"status": "no_sources", "created": 0, "updated": 0, "sources_scanned": 0}

        # Build prompt
        existing_block = ""
        if existing_people:
            lines = []
            for p in existing_people:
                lines.append(f"[{p['slug']}] {p['title']}\n{p['content'][:400]}")
            existing_block = "## EXISTING PEOPLE ENTRIES\n\n" + "\n\n".join(lines)

        source_lines = []
        for s in source_entries:
            source_lines.append(f"[{s['category']}/{s['slug']}] {s['title']}\n{s['content'][:600]}")
        source_block = "## SOURCE ENTRIES\n\n" + "\n\n".join(source_lines)

        user_prompt = source_block
        if existing_block:
            user_prompt = existing_block + "\n\n" + user_prompt

        from bob_server.services.llm_dispatch import LLMDispatchService
        llm = LLMDispatchService(self.ctx)

        response = await llm.chat(
            messages=[
                {"role": "system", "content": self._BACKFILL_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            model=llm.memory_model,
            call_category="memory_backfill",
            temperature=0.3,
            max_tokens=3000,
        )

        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            operations = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("People backfill: failed to parse LLM response")
            return {"status": "failed", "created": 0, "updated": 0,
                    "sources_scanned": len(source_entries), "raw_response": response}

        if not isinstance(operations, list):
            return {"status": "failed", "created": 0, "updated": 0,
                    "sources_scanned": len(source_entries), "raw_response": response}

        created = 0
        updated = 0
        results: list[dict[str, str]] = []
        for op in operations:
            if op.get("action") != "write":
                continue
            slug = op.get("slug", "")
            title = op.get("title", "")
            content = op.get("content", "")
            if not all([slug, title, content]):
                continue

            is_new = not (memory_dir / "core" / "people" / f"{slug}.md").is_file()
            self.write_entry(workspace_dir, "core", "people", slug, title, content)
            if is_new:
                created += 1
            else:
                updated += 1
            results.append({"slug": slug, "title": title, "source": op.get("source", ""), "is_new": str(is_new)})

        logger.info("People backfill: %d created, %d updated from %d sources",
                     created, updated, len(source_entries))
        return {
            "status": "completed",
            "created": created,
            "updated": updated,
            "sources_scanned": len(source_entries),
            "results": results,
        }

    # ── Person entries ──────────────────────────────────────────

    _CONTACT_ID_COMMENT_RE = __import__("re").compile(r"<!-- contact_id: (.+?) -->")

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
        """Create a person memory stub if one does not already exist.

        Returns the slug if created or already exists, None on failure.
        """
        memory_dir = self._memory_dir(workspace_dir)
        people_dir = memory_dir / "core" / "people"
        people_dir.mkdir(parents=True, exist_ok=True)

        # Derive slug from name
        slug = name.lower().strip()
        slug = __import__("re").sub(r"[^a-z0-9]+", "-", slug).strip("-")
        if not slug or slug.replace("-", "").isdigit():
            slug = f"contact-{contact_id[:8]}"

        # Check if entry already exists (by slug or by contact_id)
        entry_path = people_dir / f"{slug}.md"
        if entry_path.is_file():
            return slug

        # Also check if any existing entry has this contact_id
        existing_slug = self._find_slug_by_contact_id(people_dir, contact_id)
        if existing_slug:
            return existing_slug

        # Handle slug collisions by appending counter
        base_slug = slug
        counter = 2
        while entry_path.is_file():
            slug = f"{base_slug}-{counter}"
            entry_path = people_dir / f"{slug}.md"
            counter += 1

        contact_lines = []
        if phone_number:
            contact_lines.append(f"- Phone: {phone_number}")
        if email:
            contact_lines.append(f"- Email: {email}")
        if channel:
            contact_lines.append(f"- Channel: {channel}")

        content = (
            f"<!-- contact_id: {contact_id} -->\n\n"
            f"## Overview\n\n"
            f"Contact via {channel or 'unknown'}. {name}.\n\n"
            f"## Contact\n\n"
            + "\n".join(contact_lines)
        )

        self.write_entry(workspace_dir, "core", "people", slug, name, content)
        logger.info("Auto-created person entry: core/people/%s for contact %s", slug, contact_id)
        return slug

    def find_person_entry(
        self,
        workspace_dir: Path,
        *,
        contact_id: str = "",
        name: str = "",
    ) -> str | None:
        """Find a person memory entry by contact_id or name. Returns full content or None."""
        memory_dir = self._memory_dir(workspace_dir)
        people_dir = memory_dir / "core" / "people"
        if not people_dir.is_dir():
            return None

        if contact_id:
            slug = self._find_slug_by_contact_id(people_dir, contact_id)
            if slug:
                return self.read_entry(workspace_dir, "core", "people", slug)

        # Fallback: search by name in titles
        if name:
            name_lower = name.lower()
            for md_file in people_dir.glob("*.md"):
                if md_file.name.startswith("_"):
                    continue
                text = md_file.read_text(encoding="utf-8")
                title, _ = _parse_entry_summary(text)
                if title.lower() == name_lower:
                    return text

        return None

    def _find_slug_by_contact_id(self, people_dir: Path, contact_id: str) -> str | None:
        """Scan people entries for a matching contact_id metadata comment."""
        for md_file in people_dir.glob("*.md"):
            if md_file.name.startswith("_"):
                continue
            # Read only first 200 chars to find the metadata comment quickly
            head = md_file.read_text(encoding="utf-8")[:200]
            if f"contact_id: {contact_id}" in head:
                return md_file.stem
        return None

    # ── Seed ────────────────────────────────────────────────────

    @staticmethod
    def ensure_memory_structure(workspace_dir: Path) -> None:
        """Create memory/ with default config if it doesn't exist."""
        memory_dir = MemoryService._memory_dir(workspace_dir)
        if memory_dir.is_dir():
            return

        memory_dir.mkdir(parents=True, exist_ok=True)
        config_path = memory_dir / "access.yml"
        if not config_path.exists():
            config_path.write_text(_DEFAULT_ACCESS_YML, encoding="utf-8")

        config = MemoryService.load_access_config(workspace_dir)
        for wiki_name, wiki_conf in config.get("wikis", {}).items():
            for cat in wiki_conf.get("categories", []):
                (memory_dir / wiki_name / cat).mkdir(parents=True, exist_ok=True)

        logger.info("Created memory directory structure at %s", memory_dir)
