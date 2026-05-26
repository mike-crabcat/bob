"""Memory tools for LLM function calling.

Usage:
    tools.extend(make_memory_tools(ctx, session_key=session_key))
"""

from __future__ import annotations

import json
import logging
import time
from uuid import uuid4

from cyborg_server.context import AppContext
from cyborg_server.services.memory_service import MemoryService
from cyborg_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)


def make_memory_tools(ctx: AppContext, *, session_key: str) -> list[Tool]:
    """Create memory read/write/search/browse tools bound to the given context."""

    svc = MemoryService(ctx)

    @tool
    async def memory_write(
        wiki: str,
        category: str,
        slug: str,
        title: str,
        content: str,
    ) -> str:
        """Create a memory bulletin. Wiki and category must be valid per access.yml.
        Slug is a short identifier (lowercase, hyphens, no spaces). Content is markdown.
        Your write will be queued as a bulletin and curated into the right category by the dream process."""
        workspace = ctx.settings.harness.workspace_dir

        writable = await svc.resolve_writable_wikis(workspace, session_key)
        if wiki not in writable:
            return json.dumps({"error": f"Write access denied for wiki '{wiki}'"})

        if not svc.validate_wiki_category(workspace, wiki, category):
            return json.dumps({"error": f"Invalid category '{category}' for wiki '{wiki}'"})

        path = await svc.write_bulletin(
            workspace,
            session_key=session_key,
            source_type="manual",
            content=content,
            intended_category=category,
            intended_slug=slug,
            intended_title=title,
        )
        return json.dumps({"ok": True, "path": path, "queued": True})

    @tool
    async def memory_read(
        wiki: str,
        category: str,
        slug: str,
    ) -> str:
        """Read a specific memory entry by wiki, category, and slug. Returns full markdown content."""
        workspace = ctx.settings.harness.workspace_dir

        accessible = await svc.resolve_accessible_wikis(workspace, session_key)
        if wiki not in accessible:
            return json.dumps({"error": f"Access denied for wiki '{wiki}'"})

        content = svc.read_entry(workspace, wiki, category, slug)
        if content is None:
            return json.dumps({"error": f"Entry not found: {wiki}/{category}/{slug}"})
        return content

    @tool
    async def memory_search(
        query: str,
        wiki: str = "",
    ) -> str:
        """Search across memory entries. Optionally filter to a specific wiki.
        Returns an abstract summarizing findings, plus a list of matching documents
        with workspace paths and relevance explanations. Use read_file with the path
        to read the full document."""
        workspace = ctx.settings.harness.workspace_dir

        accessible = await svc.resolve_accessible_wikis(workspace, session_key)
        if wiki and wiki not in accessible:
            return json.dumps({"error": f"Access denied for wiki '{wiki}'"})

        search_wikis = [wiki] if wiki else accessible
        start = time.monotonic()
        result = await svc.search_entries(workspace, search_wikis, query)
        latency = time.monotonic() - start

        # Log the search
        try:
            await ctx.db.execute(
                "INSERT INTO memory_search_log (id, query, results_json, session_key, result_count, latency_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid4()), query, json.dumps(result), session_key, len(result.get("results", [])), latency),
            )
        except Exception:
            logger.debug("Failed to log memory search", exc_info=True)

        return json.dumps(result)

    @tool
    async def memory_browse(
        wiki: str,
        category: str,
    ) -> str:
        """List all memory entries in a wiki category. Returns slug, title, and last-modified date."""
        workspace = ctx.settings.harness.workspace_dir

        accessible = await svc.resolve_accessible_wikis(workspace, session_key)
        if wiki not in accessible:
            return json.dumps({"error": f"Access denied for wiki '{wiki}'"})

        entries = svc.browse_category(workspace, wiki, category)
        return json.dumps(entries)

    return [memory_write, memory_read, memory_search, memory_browse]
