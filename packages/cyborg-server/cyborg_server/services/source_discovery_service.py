"""Discover and manage derived project source relationships."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from cyborg_server.database import Database
from cyborg_server.exceptions import NotFoundError
from cyborg_server.models import (
    ProjectState,
    SourceOutputItem,
    SourceOutputType,
    SourceProjectResponse,
)
from cyborg_server.services.base import BaseService, json_dumps, json_loads, utcnow

logger = logging.getLogger(__name__)


def _get_projects_base_dir_from_db(db: Database) -> Path:
    """Return the configured projects base directory from runtime settings."""
    from cyborg_server.config import Settings

    settings = getattr(db, "settings", None)
    if isinstance(settings, Settings):
        return settings.projects_base_dir
    return Path("~/.openclaw/workspace/projects").expanduser()

_SCRIPT_EXTENSIONS = {".py", ".sh", ".js", ".ts", ".r", ".R"}
_VENV_DIRS = {".venv", "venv"}
_MAX_OUTPUTS_PER_SOURCE = 50


def _slugify(text: str) -> str:
    """Convert text to a URL-friendly slug (mirrors project_service._slugify)."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', '-', text)
    text = text.strip('-')
    text = re.sub(r'-+', '-', text)
    return text


class SourceDiscoveryService(BaseService):
    """Discover and manage derived project source relationships."""

    # ------------------------------------------------------------------
    # Linking / unlinking
    # ------------------------------------------------------------------

    async def link_sources(
        self,
        project_id: str,
        source_ids: list[str],
        *,
        auto_discovered: bool = False,
        relevance_data: list[dict[str, Any]] | None = None,
    ) -> list[SourceProjectResponse]:
        """Link source projects to a derived project.

        *relevance_data* is an optional list of dicts with keys
        ``project_id``, ``confidence``, ``reason`` — used when the
        caller already has LLM-provided scores.
        """
        # Build lookup for relevance data
        score_map: dict[str, tuple[float, str]] = {}
        if relevance_data:
            for item in relevance_data:
                pid = str(item.get("project_id", ""))
                score_map[pid] = (item.get("confidence", 0.0), item.get("reason", ""))

        now = utcnow().isoformat()
        linked: list[SourceProjectResponse] = []

        for source_id in source_ids:
            source_id = str(source_id)
            source = await self._get_project_row(source_id)

            # Prevent circular reference
            if await self._has_circular_reference(project_id, source_id):
                logger.warning(
                    "Skipping circular source link: %s -> %s",
                    project_id, source_id,
                )
                continue

            score, reason = score_map.get(source_id, (None, None))

            await self.db.execute(
                """
                INSERT OR IGNORE INTO project_sources
                    (derived_project_id, source_project_id, auto_discovered,
                     relevance_score, relevance_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    source_id,
                    1 if auto_discovered else 0,
                    score,
                    reason,
                    now,
                ),
            )

            linked.append(
                SourceProjectResponse(
                    source_project_id=source_id,
                    source_project_title=source["title"],
                    source_project_state=ProjectState(source["state"]),
                    auto_discovered=auto_discovered,
                    relevance_score=score,
                    relevance_reason=reason,
                    created_at=now,
                )
            )

        return linked

    async def unlink_sources(self, project_id: str, source_ids: list[str]) -> None:
        """Remove source project links."""
        for source_id in source_ids:
            await self.db.execute(
                "DELETE FROM project_sources WHERE derived_project_id = ? AND source_project_id = ?",
                (project_id, str(source_id)),
            )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def get_sources(self, project_id: str) -> list[SourceProjectResponse]:
        """Return all source projects linked to a derived project."""
        await self._get_project_row(project_id)
        rows = await self.db.fetch_all(
            """
            SELECT ps.*, p.title AS source_title, p.state AS source_state
            FROM project_sources ps
            INNER JOIN projects p ON p.id = ps.source_project_id AND p.deleted_at IS NULL
            WHERE ps.derived_project_id = ?
            ORDER BY ps.created_at
            """,
            (project_id,),
        )
        return [
            SourceProjectResponse(
                source_project_id=row["source_project_id"],
                source_project_title=row["source_title"],
                source_project_state=ProjectState(row["source_state"]),
                auto_discovered=bool(row["auto_discovered"]),
                relevance_score=row["relevance_score"],
                relevance_reason=row["relevance_reason"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Output scanning
    # ------------------------------------------------------------------

    async def scan_source_outputs(self, project_id: str) -> list[SourceOutputItem]:
        """Scan all linked source projects for usable outputs.

        Results are cached in the derived project's metadata under the
        ``derived_outputs`` key so subsequent reads don't need to hit the
        filesystem again.
        """
        sources = await self.get_sources(project_id)
        if not sources:
            return []

        all_outputs: list[SourceOutputItem] = []

        for source in sources:
            outputs = await self._scan_project_outputs(
                str(source.source_project_id),
                source.source_project_title,
            )
            all_outputs.extend(outputs)

        # Cache in project metadata
        await self._cache_outputs(project_id, all_outputs)
        return all_outputs

    async def _scan_project_outputs(
        self,
        source_project_id: str,
        source_title: str,
    ) -> list[SourceOutputItem]:
        """Scan a single source project for outputs."""
        outputs: list[SourceOutputItem] = []

        # 1. Registered task files from the database
        file_rows = await self.db.fetch_all(
            "SELECT filename, relative_path, purpose, task_id, size_bytes FROM task_files WHERE project_id = ? ORDER BY created_at ASC",
            (source_project_id,),
        )
        for row in file_rows:
            purpose = row["purpose"]
            output_type = self._map_purpose_to_output_type(purpose)
            outputs.append(
                SourceOutputItem(
                    output_type=output_type,
                    path=row["relative_path"],
                    description=f"{row['filename']} ({purpose})",
                    size_bytes=row["size_bytes"],
                    source_project_id=source_project_id,
                    source_task_id=row.get("task_id"),
                )
            )

        # 2. Filesystem scan of the source project workspace
        slug = _slugify(source_title)
        workspace = _get_projects_base_dir_from_db(self.db) / slug
        if workspace.exists():
            # Venv
            for venv_name in _VENV_DIRS:
                venv_path = workspace / venv_name
                if venv_path.is_dir():
                    outputs.append(
                        SourceOutputItem(
                            output_type=SourceOutputType.VENV,
                            path=str(venv_path),
                            description=f"Python virtual environment ({venv_name})",
                            source_project_id=source_project_id,
                        )
                    )
                    break  # Only record one venv

            # SUMMARY.md
            summary_path = workspace / "SUMMARY.md"
            if summary_path.exists():
                outputs.append(
                    SourceOutputItem(
                        output_type=SourceOutputType.SUMMARY,
                        path=str(summary_path),
                        description="Project summary",
                        source_project_id=source_project_id,
                    )
                )

            # Top-level scripts
            try:
                for entry in os.listdir(workspace):
                    entry_path = workspace / entry
                    if entry_path.is_file() and entry_path.suffix in _SCRIPT_EXTENSIONS:
                        outputs.append(
                            SourceOutputItem(
                                output_type=SourceOutputType.SCRIPT,
                                path=str(entry_path),
                                description=f"Script: {entry}",
                                source_project_id=source_project_id,
                            )
                        )
            except OSError:
                pass

            # Task RESULT.md files
            tasks_dir = workspace / "tasks"
            if tasks_dir.is_dir():
                for task_dir in tasks_dir.iterdir():
                    if task_dir.is_dir():
                        result_path = task_dir / "RESULT.md"
                        if result_path.exists():
                            outputs.append(
                                SourceOutputItem(
                                    output_type=SourceOutputType.RESULT,
                                    path=str(result_path),
                                    description=f"Task result ({task_dir.name})",
                                    source_project_id=source_project_id,
                                )
                            )

        # Cap per-source
        return outputs[:_MAX_OUTPUTS_PER_SOURCE]

    @staticmethod
    def _map_purpose_to_output_type(purpose: str) -> SourceOutputType:
        mapping = {
            "reasoning": SourceOutputType.REPORT,
            "result": SourceOutputType.RESULT,
            "analysis": SourceOutputType.REPORT,
            "log": SourceOutputType.OTHER,
            "artifact": SourceOutputType.ARTIFACT,
            "other": SourceOutputType.OTHER,
        }
        return mapping.get(purpose, SourceOutputType.OTHER)

    # ------------------------------------------------------------------
    # Auto-discovery via LLM
    # ------------------------------------------------------------------

    async def auto_discover_sources(
        self,
        project_id: str,
        aim: str | None,
        method: str | None,
    ) -> list[SourceProjectResponse]:
        """Use LLM reasoning to find relevant closed projects as sources.

        Fails gracefully — returns an empty list on any error so project
        creation is never blocked.
        """
        if not aim:
            return []

        # Fetch closed projects with brief summaries
        rows = await self.db.fetch_all(
            """
            SELECT p.id, p.title, p.aim, p.method, p.conclusion,
                   (SELECT COUNT(*) FROM task_files tf WHERE tf.project_id = p.id) AS file_count
            FROM projects p
            WHERE p.state = ? AND p.deleted_at IS NULL
            ORDER BY p.closed_at DESC
            """,
            (ProjectState.CLOSED.value,),
        )

        if not rows:
            return []

        closed_projects = [dict(r) for r in rows]

        # Exclude the project itself (in case it was somehow already closed)
        closed_projects = [p for p in closed_projects if p["id"] != project_id]

        if not closed_projects:
            return []

        try:
            from cyborg_server.services.openclaw_reasoning_service import OpenClawReasoningService
            reasoning = OpenClawReasoningService(self.db)
            matches = await reasoning.discover_sources(
                aim=aim,
                method=method,
                closed_projects=closed_projects,
            )
        except Exception as exc:
            logger.warning("Source auto-discovery failed for project %s: %s", project_id, exc)
            return []

        if not matches:
            return []

        # Cap at 5 auto-discovered sources
        matches = matches[:5]

        # Link the discovered sources
        source_ids = [m["project_id"] for m in matches]
        return await self.link_sources(
            project_id,
            source_ids,
            auto_discovered=True,
            relevance_data=matches,
        )

    # ------------------------------------------------------------------
    # Context for prompts
    # ------------------------------------------------------------------

    async def get_derived_outputs_for_context(self, project_id: str) -> dict[str, Any]:
        """Return a structured dict for prompt injection."""
        sources = await self.get_sources(project_id)
        if not sources:
            return {}

        outputs = await self._get_cached_outputs(project_id)
        if not outputs:
            # No cached outputs yet — scan now
            outputs = await self.scan_source_outputs(project_id)

        source_details = []
        for source in sources:
            source_outputs = [
                o for o in outputs
                if str(o.source_project_id) == str(source.source_project_id)
            ]
            source_row = await self.db.fetch_one(
                "SELECT aim, method, conclusion FROM projects WHERE id = ?",
                (str(source.source_project_id),),
            )
            source_details.append({
                "id": str(source.source_project_id),
                "title": source.source_project_title,
                "aim": source_row["aim"] if source_row else None,
                "method": source_row["method"] if source_row else None,
                "conclusion": source_row["conclusion"] if source_row else None,
                "relevance_reason": source.relevance_reason,
                "outputs": [
                    {
                        "type": o.output_type.value,
                        "path": o.path,
                        "description": o.description,
                    }
                    for o in source_outputs
                ],
            })

        return {"source_projects": source_details}

    # ------------------------------------------------------------------
    # Caching helpers
    # ------------------------------------------------------------------

    async def _get_cached_outputs(self, project_id: str) -> list[SourceOutputItem]:
        """Read derived_outputs from project metadata JSON."""
        row = await self.db.fetch_one(
            "SELECT metadata FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_id,),
        )
        if not row:
            return []
        metadata = json_loads(row.get("metadata"), {})
        raw = metadata.get("derived_outputs", [])
        return [SourceOutputItem.model_validate(item) for item in raw]

    async def _cache_outputs(self, project_id: str, outputs: list[SourceOutputItem]) -> None:
        """Write derived_outputs into project metadata JSON."""
        row = await self.db.fetch_one(
            "SELECT metadata FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_id,),
        )
        if not row:
            return
        metadata = json_loads(row.get("metadata"), {})
        metadata["derived_outputs"] = [o.model_dump(mode="json") for o in outputs]
        await self.db.execute(
            "UPDATE projects SET metadata = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (json_dumps(metadata), utcnow().isoformat(), project_id),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_project_row(self, project_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one(
            "SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_id,),
        )
        if row is None:
            raise NotFoundError(f"Project '{project_id}' was not found")
        return row

    async def _has_circular_reference(self, derived_id: str, source_id: str) -> bool:
        """Check if source_id already derives from derived_id."""
        row = await self.db.fetch_one(
            "SELECT 1 FROM project_sources WHERE derived_project_id = ? AND source_project_id = ?",
            (source_id, derived_id),
        )
        return row is not None
