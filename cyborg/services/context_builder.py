"""Build intelligent context for LLM reasoning about projects and tasks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from cyborg.database import Database
from cyborg.services.base import BaseService, json_dumps, json_loads, utcnow


class ContextScope(StrEnum):
    """How much context to include."""
    MINIMAL = "minimal"       # Just current state, 1-2k tokens
    STANDARD = "standard"     # Recent + key items, 5-10k tokens
    COMPREHENSIVE = "comprehensive"  # Everything relevant, 20-30k tokens
    FULL = "full"            # All context (rare, for deep analysis)


class ContextBuilder(BaseService):
    """Build intelligent context for LLM reasoning."""

    async def build_project_context(
        self,
        project_id: str,
        scope: ContextScope = ContextScope.STANDARD,
        focus_reasoning: str | None = None,  # "planning", "evaluation", "refinement", "learning"
    ) -> dict[str, Any]:
        """
        Build context dict optimized for LLM consumption.

        Args:
            project_id: Project to build context for
            scope: How much context to include
            focus_reasoning: What type of reasoning this is for (affects what's relevant)

        Returns:
            Structured context ready for LLM prompt
        """

        # 1. Core project context (always included)
        core_context = await self._get_core_context(project_id)

        # 2. Task context (filtered by scope)
        task_context = await self._get_task_context(
            project_id,
            scope,
            focus_reasoning
        )

        # 3. Journal narrative (summarized if large)
        journal_context = await self._get_journal_context(
            project_id,
            scope
        )

        # 4. Temporal context
        temporal_context = await self._get_temporal_context(project_id)

        # 5. Related context (if needed)
        related_context = await self._get_related_context(
            project_id,
            scope
        )

        # 6. Build the prompt-ready structure
        return self._assemble_context(
            core=core_context,
            tasks=task_context,
            journal=journal_context,
            temporal=temporal_context,
            related=related_context,
            scope=scope,
        )

    async def _get_core_context(self, project_id: str) -> dict[str, Any]:
        """Core project metadata and objectives."""

        project = await self.db.fetch_one(
            """
            SELECT id, title, aim, method, state, plan, success_criteria,
                   conclusion, created_at, started_at, paused_at, closed_at,
                   metadata, auto_execute
            FROM projects
            WHERE id = ? AND deleted_at IS NULL
            """,
            (project_id,)
        )

        if not project:
            return {"project": {}}

        # Parse plan and success criteria
        plan = self._parse_json_field(project, "plan")
        success_criteria = self._parse_json_field(project, "success_criteria")

        return {
            "project": {
                "id": project["id"],
                "title": project["title"],
                "aim": project.get("aim"),
                "method": project.get("method"),
                "state": project["state"],
                "is_auto_executing": bool(project.get("auto_execute", 0)),
                "started_at": project.get("started_at"),
                "duration_days": self._calculate_duration(project),
            },
            "plan": {
                "total_steps": len(plan),
                "steps": plan if len(plan) <= 10 else self._summarize_long_plan(plan),
            },
            "success_criteria": {
                "total_count": len(success_criteria),
                "criteria": success_criteria,
            },
        }

    async def _get_task_context(
        self,
        project_id: str,
        scope: ContextScope,
        focus_reasoning: str | None
    ) -> dict[str, Any]:
        """Task state and results, filtered by relevance."""

        # Get all tasks with their linkage
        tasks = await self.db.fetch_all(
            """
            SELECT
                t.id, t.title, t.description, t.status, t.priority,
                t.started_at, t.completed_at, t.result,
                t.metadata, t.parent_id, t.created_at, t.updated_at
            FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.deleted_at IS NULL
            ORDER BY t.created_at
            """,
            (project_id,)
        )

        # Filter based on scope and reasoning type
        filtered_tasks = self._filter_tasks_by_scope(
            [dict(t) for t in tasks],
            scope,
            focus_reasoning
        )

        # Attach output files for non-minimal scopes
        if scope != ContextScope.MINIMAL:
            for task in filtered_tasks:
                try:
                    file_rows = await self.db.fetch_all(
                        "SELECT filename, relative_path, purpose FROM task_files WHERE task_id = ? ORDER BY created_at ASC",
                        (task["id"],),
                    )
                    task["output_files"] = [dict(r) for r in file_rows] if file_rows else []
                except Exception:
                    task["output_files"] = []

        return {
            "summary": self._summarize_task_state(filtered_tasks),
            "tasks": self._format_tasks_for_scope(filtered_tasks, scope),
            "recent_results": self._get_recent_task_results(filtered_tasks, limit=5),
        }

    def _filter_tasks_by_scope(
        self,
        tasks: list[dict[str, Any]],
        scope: ContextScope,
        focus_reasoning: str | None
    ) -> list[dict[str, Any]]:
        """Filter tasks based on scope and reasoning focus."""

        if scope == ContextScope.FULL:
            return tasks

        if scope == ContextScope.MINIMAL:
            # Only active and recently completed
            cutoff = utcnow() - timedelta(hours=24)
            return [
                t for t in tasks
                if t["status"] in ["active", "pending"]
                or (
                    self._coerce_datetime(t.get("completed_at")) is not None
                    and self._coerce_datetime(t.get("completed_at")) > cutoff
                )
            ]

        # STANDARD / COMPREHENSIVE
        filtered = []

        # Always include active and pending tasks
        filtered.extend([t for t in tasks if t["status"] in ["active", "pending"]])

        # Include recently completed (last 7 days for standard, 14 for comprehensive)
        days = 14 if scope == ContextScope.COMPREHENSIVE else 7
        cutoff = utcnow() - timedelta(days=days)
        filtered.extend([
            t for t in tasks
            if (
                t["status"] == "completed"
                and self._coerce_datetime(t.get("completed_at")) is not None
                and self._coerce_datetime(t.get("completed_at")) > cutoff
            )
        ])

        # For specific reasoning types, include specific task sets
        if focus_reasoning == "evaluation":
            # Include failed tasks for evaluation
            filtered.extend([t for t in tasks if t["status"] == "failed"])

        elif focus_reasoning == "refinement":
            # Include parent tasks to understand dependencies
            task_ids = {t["id"] for t in filtered}
            for t in tasks:
                if t.get("parent_id") in task_ids:
                    filtered.append(t)

        # Deduplicate while preserving order
        seen = set()
        unique_filtered = []
        for t in filtered:
            if t["id"] not in seen:
                seen.add(t["id"])
                unique_filtered.append(t)

        return unique_filtered

    def _format_tasks_for_scope(
        self,
        tasks: list[dict[str, Any]],
        scope: ContextScope
    ) -> list[dict[str, Any]]:
        """Format tasks based on scope - full detail or summarized."""

        if scope in [ContextScope.COMPREHENSIVE, ContextScope.FULL]:
            # Full detail
            return tasks

        # Standard/minimal - summarized
        return self._summarize_tasks(tasks)

    def _summarize_tasks(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Summarize tasks to reduce token count."""

        return [
            {
                "id": t["id"],
                "title": t["title"],
                "status": t["status"],
                "priority": t["priority"],
                "completed_at": t.get("completed_at"),
                "result_summary": t.get("result", "")[:200] if t.get("result") else None,
            }
            for t in tasks
        ]

    def _summarize_task_state(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        """Generate summary statistics."""

        return {
            "total": len(tasks),
            "pending": len([t for t in tasks if t["status"] == "pending"]),
            "active": len([t for t in tasks if t["status"] == "active"]),
            "completed": len([t for t in tasks if t["status"] == "completed"]),
            "failed": len([t for t in tasks if t["status"] == "failed"]),
            "blocked": len([t for t in tasks if t["status"] == "blocked"]),
        }

    def _get_recent_task_results(
        self,
        tasks: list[dict[str, Any]],
        limit: int = 5
    ) -> list[dict[str, Any]]:
        """Get recent completed task results."""

        completed = [t for t in tasks if t["status"] == "completed" and t.get("result")]
        completed.sort(key=lambda t: self._coerce_datetime(t.get("completed_at")) or utcnow(), reverse=True)
        return completed[:limit]

    async def _get_journal_context(
        self,
        project_id: str,
        scope: ContextScope
    ) -> dict[str, Any]:
        """Journal entries - the project narrative."""

        entries = await self.db.fetch_all(
            """
            SELECT entry_type, content, created_at, metadata
            FROM project_journal_entries
            WHERE project_id = ?
            ORDER BY created_at ASC
            """,
            (project_id,)
        )

        all_entries = [dict(e) for e in entries]

        if scope == ContextScope.MINIMAL:
            # Just recent milestones and blockers
            cutoff = utcnow() - timedelta(days=3)
            return {
                "entries": [
                    e for e in all_entries
                    if e["entry_type"] in ["milestone", "blocker"]
                    and self._coerce_datetime(e.get("created_at")) is not None
                    and self._coerce_datetime(e.get("created_at")) > cutoff
                ],
                "total_entries": len(all_entries),
                "summarized": True,
            }

        if scope == ContextScope.STANDARD:
            # Recent entries + all milestones/decisions/blockers
            cutoff = utcnow() - timedelta(days=14)

            important = [e for e in all_entries if e["entry_type"] in ["milestone", "decision", "blocker"]]
            recent = [
                e for e in all_entries
                if self._coerce_datetime(e.get("created_at")) is not None
                and self._coerce_datetime(e.get("created_at")) > cutoff
            ]

            # Combine and deduplicate by content
            combined = {e["content"]: e for e in important + recent}.values()

            return {
                "entries": list(combined),
                "total_entries": len(all_entries),
                "summarized": len(all_entries) > len(combined),
            }

        # COMPREHENSIVE / FULL
        if len(all_entries) > 50:
            # Summarize older entries
            return {
                "entries": all_entries[-30:],  # Last 30 entries
                "early_summary": self._summarize_early_journal(all_entries[:-30]),
                "total_entries": len(all_entries),
                "summarized": True,
            }

        return {
            "entries": all_entries,
            "total_entries": len(all_entries),
            "summarized": False,
        }

    def _summarize_early_journal(self, entries: list[dict[str, Any]]) -> str:
        """Summarize old journal entries into a narrative."""

        if not entries:
            return "No early journal entries."

        # Group by type
        by_type: dict[str, int] = {}
        for e in entries:
            entry_type = e["entry_type"]
            by_type[entry_type] = by_type.get(entry_type, 0) + 1

        parts = []
        for entry_type, count in sorted(by_type.items()):
            parts.append(f"{count} {entry_type} entries")

        return f"Early project history: {', '.join(parts)}"

    async def _get_temporal_context(self, project_id: str) -> dict[str, Any]:
        """Time-based context and deadlines."""

        project = await self.db.fetch_one(
            "SELECT created_at, started_at, closed_at FROM projects WHERE id = ?",
            (project_id,)
        )

        if not project:
            return {"current_timestamp": utcnow().isoformat()}

        # Get upcoming calendar events
        try:
            events = await self.db.fetch_all(
                """
                SELECT e.title, e.start_time, e.venue
                FROM events e
                INNER JOIN project_events pe ON pe.event_id = e.id
                WHERE pe.project_id = ?
                  AND e.start_time > datetime('now')
                  AND e.deleted_at IS NULL
                ORDER BY e.start_time
                LIMIT 5
                """,
                (project_id,)
            )
        except Exception:
            # Older or partial test databases may not include project_events yet.
            events = []

        return {
            "project_age_days": self._calculate_days_since(project.get("created_at")),
            "active_duration_days": self._calculate_days_since(project.get("started_at")),
            "upcoming_events": [dict(e) for e in events],
            "current_timestamp": utcnow().isoformat(),
        }

    async def _get_related_context(
        self,
        project_id: str,
        scope: ContextScope
    ) -> dict[str, Any]:
        """Related entities and dependencies."""

        # For now, minimal - can be expanded later
        return {
            "notes": "Related context not yet implemented"
        }

    def _assemble_context(
        self,
        core: dict[str, Any],
        tasks: dict[str, Any],
        journal: dict[str, Any],
        temporal: dict[str, Any],
        related: dict[str, Any],
        scope: ContextScope,
    ) -> dict[str, Any]:
        """Assemble final context structure."""

        return {
            "scope": scope,
            "generated_at": utcnow().isoformat(),
            "core": core,
            "tasks": tasks,
            "journal": journal,
            "temporal": temporal,
            "related": related,
            "metadata": {
                "total_estimated_tokens": self._estimate_tokens(core, tasks, journal),
            },
        }

    def _estimate_tokens(
        self,
        core: dict,
        tasks: dict,
        journal: dict
    ) -> int:
        """Rough token estimation for context sizing."""

        # Rough estimate: 1 token ≈ 4 characters
        import json

        total_chars = (
            len(json.dumps(core)) +
            len(json.dumps(tasks)) +
            len(json.dumps(journal))
        )

        return total_chars // 4

    def _parse_json_field(self, row: dict[str, Any] | None, field: str) -> list[Any]:
        """Parse a JSON field from a database row."""

        if not row:
            return []
        value = row.get(field)
        if not value:
            return []

        try:
            data = json_loads(value, [])
            if isinstance(data, list):
                return data
        except Exception:
            pass

        return []

    def _summarize_long_plan(self, plan: list[Any]) -> list[dict[str, Any]]:
        """Summarize a long plan to first/last/middle."""

        if len(plan) <= 10:
            return plan

        return [
            *plan[:3],  # First 3
            {"title": f"... {len(plan) - 6} more steps ...", "summary": True},
            *plan[-3:],  # Last 3
        ]

    def _calculate_duration(self, project: dict[str, Any] | None) -> int | None:
        """Calculate project duration in days."""

        if not project:
            return None

        started = project.get("started_at")
        closed = project.get("closed_at")

        if not started:
            return None

        end = closed or utcnow()

        # Parse timestamps if they're strings
        if isinstance(started, str):
            started = datetime.fromisoformat(started)
        if isinstance(end, str):
            end = datetime.fromisoformat(end)

        return (end - started).days

    def _calculate_days_since(self, timestamp: str | datetime | None) -> int | None:
        """Calculate days since a timestamp."""

        parsed = self._coerce_datetime(timestamp)
        if parsed is None:
            return None

        return (utcnow() - parsed).days

    def _coerce_datetime(self, timestamp: str | datetime | None) -> datetime | None:
        """Parse an ISO timestamp into an aware UTC datetime when possible."""

        if not timestamp:
            return None
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)
