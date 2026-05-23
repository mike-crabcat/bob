"""Docs search service — query project documentation via LLM delegation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from cyborg_server.services.base import BaseService

logger = logging.getLogger(__name__)


class DocsService(BaseService):
    """Search project docs files using an LLM to extract relevant passages."""

    @staticmethod
    def discover_docs(workspace_dir: Path) -> list[dict[str, str]]:
        """Glob docs/**/*.md and return metadata dicts."""
        docs_dir = workspace_dir / "docs"
        if not docs_dir.is_dir():
            return []

        results: list[dict[str, str]] = []
        for md_file in sorted(docs_dir.rglob("*.md")):
            try:
                text = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            rel = md_file.relative_to(workspace_dir)
            title = ""
            for line in text.strip().splitlines():
                stripped = line.strip()
                if stripped.startswith("# "):
                    title = stripped[2:].strip()
                    break
            results.append({
                "path": str(rel),
                "filename": md_file.name,
                "title": title or md_file.stem,
                "content": text,
            })
        return results

    async def search_docs(self, workspace_dir: Path, query: str) -> dict[str, Any]:
        """Search docs using LLM delegation.

        Returns {"abstract": str, "sections": [{"doc_path", "title", "passage", "relevance"}]}.
        """
        all_docs = self.discover_docs(workspace_dir)

        if not all_docs:
            return {"abstract": "No documentation files found.", "sections": []}

        # Build numbered catalog with full content
        catalog_lines: list[str] = []
        for i, doc in enumerate(all_docs):
            catalog_lines.append(
                f"[{i}] {doc['path']} ({doc['title']}, {len(doc['content'].splitlines())} lines)\n"
                f"    Content:\n"
                + "\n".join(f"    {line}" for line in doc["content"].splitlines())
            )
        catalog = "\n\n".join(catalog_lines)

        system_prompt = (
            "You are a documentation search agent. Given a query and a catalog of documentation files, "
            "find passages relevant to the query and explain their relevance.\n\n"
            "Return a JSON object with exactly these keys:\n"
            '- "abstract": 1-2 sentence summary answering the query based on the docs\n'
            '- "sections": array of objects, each with:\n'
            '    "index": integer (matching the [N] in the catalog)\n'
            '    "passage": a direct quote of the relevant passage from the doc (up to 500 chars)\n'
            '    "relevance": one sentence explaining why this passage answers the query\n'
            '\nReturn ONLY valid JSON. Return {"abstract": "No relevant documentation found.", "sections": []} if nothing matches.'
        )
        user_prompt = f"Query: {query}\n\nCatalog:\n{catalog}"

        from cyborg_server.services.llm_dispatch import LLMDispatchService

        llm = LLMDispatchService(self.ctx)
        response = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            call_category="docs_search",
            temperature=0.0,
            max_tokens=800,
        )

        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(text)
            abstract = parsed.get("abstract", "")
            raw_sections = parsed.get("sections", [])
        except (json.JSONDecodeError, ValueError):
            # Fallback: keyword matching
            abstract = "Keyword fallback results."
            raw_sections = [
                {"index": i, "passage": doc["content"][:300], "relevance": "Keyword match"}
                for i, doc in enumerate(all_docs)
                if query.lower() in doc["content"].lower()
            ]

        sections: list[dict[str, str]] = []
        for item in raw_sections:
            idx = item.get("index") if isinstance(item, dict) else item
            if not isinstance(idx, int) or idx < 0 or idx >= len(all_docs):
                continue
            doc = all_docs[idx]
            sections.append({
                "doc_path": doc["path"],
                "title": doc["title"],
                "passage": item.get("passage", "") if isinstance(item, dict) else "",
                "relevance": item.get("relevance", "") if isinstance(item, dict) else "",
            })

        return {"abstract": abstract, "sections": sections}
