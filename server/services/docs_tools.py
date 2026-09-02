"""Docs tools for LLM function calling.

Usage:
    tools.extend(make_docs_tools(ctx, session_key=session_key))
"""

from __future__ import annotations

import json
import logging

from bob_server.context import AppContext
from bob_server.services.docs_service import DocsService
from bob_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)


def make_docs_tools(ctx: AppContext, *, session_key: str) -> list[Tool]:
    """Create docs search tool bound to the given context."""

    svc = DocsService(ctx)

    @tool
    async def docs_search(query: str) -> str:
        """Search bob's documentation for information about features, architecture,
        or how the system works. Returns relevant passages from docs files.
        Use this when you need to understand how a feature works, find configuration
        details, or look up system behavior."""
        workspace = ctx.settings.harness.workspace_dir
        result = await svc.search_docs(workspace, query)
        return json.dumps(result)

    return [docs_search]
