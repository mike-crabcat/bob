"""Persistent memory wiki — markdown files with auto-generated indexes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

from cyborg_server.services.base import BaseService

logger = logging.getLogger(__name__)

_DEFAULT_ACCESS_YML = """\
wikis:
  core:
    description: "General knowledge"
    categories: [people, facts, events, locations, research]
    access: always
    write: always
"""

_config_cache: tuple[float, dict[str, Any]] | None = None  # (mtime, parsed config)


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

        from cyborg_server.services.llm_dispatch import LLMDispatchService

        llm = LLMDispatchService(self.ctx)
        response = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
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

    # ── Reflection ──────────────────────────────────────────────

    async def reflect_and_update(
        self,
        workspace_dir: Path,
        session_key: str,
        summary_text: str,
        memory_prompts: list[str],
    ) -> None:
        """Review conversation summary and update memory entries."""
        if not memory_prompts:
            return

        accessible = await self.resolve_accessible_wikis(workspace_dir, session_key)
        writable = await self.resolve_writable_wikis(workspace_dir, session_key)
        if not writable:
            return

        current_index = self.build_memory_index(workspace_dir, accessible)
        config = self.load_access_config(workspace_dir)

        writable_categories: dict[str, list[str]] = {}
        for wiki_name in writable:
            wiki_conf = config.get("wikis", {}).get(wiki_name, {})
            writable_categories[wiki_name] = wiki_conf.get("categories", [])

        system_prompt = (
            "You are a memory update agent. Review the conversation summary and update the memory wiki.\n"
            "Return a JSON array of operations:\n"
            '[{"action": "write", "wiki": "...", "category": "...", "slug": "...", "title": "...", "content": "..."}]\n'
            "Rules:\n"
            "- Only write to wikis and categories listed below\n"
            "- Use descriptive slugs (lowercase, hyphens)\n"
            "- Content is markdown, keep it concise\n"
            "- Only create entries for genuinely useful, durable information\n"
            "- If an entry updates an existing one, use the same slug\n"
            "- Choose the correct category for each entry:\n"
            "  - people: information about a specific person (preferences, relationships, personality, contact details)\n"
            "  - events: things that happened at a specific time (appointments, milestones, incidents)\n"
            "  - facts: general knowledge and standing facts (procedures, preferences, how-tos)\n"
            "  - locations: places and their details\n"
            "  - research: findings, notes, and investigation results\n"
            "\n"
            f"Writable wikis and categories:\n"
            + "\n".join(
                f"- {wiki}: {', '.join(cats)}" for wiki, cats in writable_categories.items()
            )
            + "\n\nCurrent memory index:\n"
            + (current_index or "(empty)")
        )

        user_prompt = (
            f"Conversation summary: {summary_text}\n\n"
            f"Items to remember:\n"
            + "\n".join(f"- {p}" for p in memory_prompts)
        )

        from cyborg_server.services.llm_dispatch import LLMDispatchService

        llm = LLMDispatchService(self.ctx)
        response = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            call_category="memory_reflection",
            temperature=0.3,
            max_tokens=1000,
        )

        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            operations = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Memory reflection: failed to parse LLM response")
            return

        if not isinstance(operations, list):
            return

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
            if wiki not in writable:
                continue
            if not self.validate_wiki_category(workspace_dir, wiki, category):
                continue

            self.write_entry(workspace_dir, wiki, category, slug, title, content)
            logger.info("Memory reflection wrote: %s/%s/%s", wiki, category, slug)

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
