"""Business logic for self-executing project management.

This module provides the core execution engine for projects that can
auto-progress through their plan steps and auto-complete when success
criteria are met.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import uuid4

from aiosqlite import Connection

from cyborg_server.database import Database
from cyborg_server.exceptions import ConflictError, NotFoundError
from cyborg_server.models import (
    JournalEntryType,
    PlanStep,
    ProjectResponse,
    ProjectState,
    SuccessCriterion,
    TaskCreate,
    TaskPriority,
    TaskStatus,
)
from cyborg_server.services.base import BaseService, json_dumps, json_loads, utcnow
from cyborg_server.services.notification_service import NotificationService
from cyborg_server.services.project_spec_service import ProjectSpecService
from cyborg_server.services.webhook_service import WebhookEvent, WebhookService


logger = logging.getLogger(__name__)

# Lazy import structured logging helpers
_structured_logger = None


def _get_structured_logger():
    """Lazy import structured logging helpers."""
    global _structured_logger
    if _structured_logger is None:
        from cyborg_server.structured_logging import get_logger as _get_logger
        _structured_logger = _get_logger(__name__)
    return _structured_logger


class ProjectExecutionService(BaseService):
    """Service for managing self-executing project workflows."""

    def __init__(self, db: Database, task_service: Any | None = None, webhook_service: WebhookService | None = None) -> None:
        super().__init__(db)
        self._task_service = task_service
        self._webhook_service = webhook_service
        self._project_spec_service: ProjectSpecService | None = None
        self._reasoning_service = None

    @property
    def task_service(self) -> Any:
        """Lazy-load task service to avoid circular dependencies."""
        if self._task_service is None:
            from cyborg_server.services.task_service import TaskService
            self._task_service = TaskService(self.db)
        return self._task_service

    def _get_webhook_service(self) -> WebhookService | None:
        """Lazy-load webhook service."""
        if self._webhook_service is None:
            self._webhook_service = WebhookService(self.db)
        return self._webhook_service

    @property
    def project_spec_service(self) -> ProjectSpecService:
        """Lazy-load project spec service."""
        if self._project_spec_service is None:
            self._project_spec_service = ProjectSpecService(self.db)
        return self._project_spec_service

    @property
    def reasoning_service(self) -> Any:
        """Lazy-load OpenClaw reasoning service."""
        if self._reasoning_service is None:
            from cyborg_server.services.openclaw_reasoning_service import OpenClawReasoningService

            self._reasoning_service = OpenClawReasoningService(self.db)
        return self._reasoning_service

    async def _sync_notifications(self, project_id: str) -> None:
        await NotificationService(self.db).sync_project_state(project_id)

    async def on_project_resumed(self, project_id: str, *, resumed_from_block: bool = False) -> None:
        """Resume reasoning after a project is unpaused.

        Looks at the current task state and triggers the appropriate next step:
        - If all tasks are completed, runs decide_next_action to create more or close
        - If there are no tasks, starts execution from the plan
        - Active/submitted/pending tasks should already have their notifications

        When resumed_from_block is True (project was just unblocked after user approval),
        tries auto-completion first before asking reasoning, to prevent re-blocking loops.
        """
        project = await self._get_project_row(project_id)
        if not project or project["state"] != ProjectState.ACTIVE.value:
            return

        latest_task = await self.db.fetch_one(
            """
            SELECT t.id, t.title, t.status FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.deleted_at IS NULL
            ORDER BY t.created_at DESC LIMIT 1
            """,
            (project_id,),
        )

        if latest_task and latest_task["status"] == TaskStatus.COMPLETED.value:
            # When resuming from a block with all tasks done, try auto-completion first
            if resumed_from_block:
                try:
                    result = await self.evaluate_and_complete(project_id)
                    if result is not None:
                        return  # Auto-completed, no reasoning needed
                except Exception:
                    pass  # Fall through to decide_next_action
            await self.decide_next_action(
                project_id, latest_task["id"], latest_task["title"],
                resumed_from_block=resumed_from_block,
            )
        elif not latest_task:
            await self.start_project_execution(project_id)

    async def on_task_completed(self, task_id: str, task_title: str, result_summary: str | None = None) -> list[ProjectResponse]:
        """Hook called when a task is completed.

        For auto-executing projects, invokes reasoning to decide the next action.
        Returns list of projects that were affected.
        """
        project_ids = await self._get_project_ids_for_task(task_id)
        affected_projects: list[ProjectResponse] = []

        for project_id in project_ids:
            project = await self._get_project_row(project_id)
            if not project or project["state"] != ProjectState.ACTIVE.value:
                continue

            # Invoke reasoning to decide next action
            await self.decide_next_action(project_id, task_id, task_title, result_summary)
            affected_projects.append(await self._build_project_response(project))

        return affected_projects

    async def start_project_execution(self, project_id: str) -> ProjectResponse:
        """Start auto-execution for a project.

        This will:
        1. Transition project to ACTIVE state
        2. Create the first task for step 0
        3. Store subagent session key if provided
        """
        project = await self._get_project_row(project_id)
        if not project:
            raise NotFoundError(f"Project '{project_id}' was not found")
        await self.project_spec_service.ensure_project_ready_for_execution(project_id)

        now = utcnow().isoformat()
        previous_state = project["state"]
        previous_started_at = project.get("started_at")

        # Preserve original started_at when resuming from paused
        started_at = previous_started_at if previous_state == ProjectState.PAUSED.value else now

        # Update project state
        from cyborg_server.services.project_service import short_task_id
        subagent_key = f"cyborg:project:{short_task_id(project_id)}"
        await self.db.execute(
            """
            UPDATE projects
            SET state = ?, started_at = ?, subagent_session_key = ?, updated_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (ProjectState.ACTIVE.value, started_at, subagent_key, now, project_id),
        )

        plan = self._parse_plan(project.get("plan"))
        try:
            if plan:
                await self._create_initial_task(project_id, plan[0])
        except Exception:
            rollback_now = utcnow().isoformat()
            await self.db.execute(
                """
                UPDATE projects
                SET state = ?, started_at = ?, updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (previous_state, previous_started_at, rollback_now, project_id),
            )
            raise

        await self._sync_notifications(project_id)
        return await self._build_project_response(await self._get_project_row(project_id))

    async def evaluate_and_complete(self, project_id: str) -> ProjectResponse | None:
        """
        Evaluate success criteria using OpenClaw reasoning and auto-complete project if all criteria met.

        This is used for manual evaluation via the API endpoint.
        The automatic flow uses decide_next_action instead.
        Returns the completed project if auto-completed, None otherwise.
        """
        from cyborg_server.structured_logging import log_autonomy_decision

        project = await self._get_project_row(project_id)
        if not project or project["state"] != ProjectState.ACTIVE.value:
            return None

        success_criteria = self._parse_success_criteria(project.get("success_criteria"))
        if not success_criteria:
            return None

        # Use OpenClaw reasoning for evaluation
        try:
            evaluation = await self.reasoning_service.evaluate_success_criteria(project_id)
        except Exception as e:
            logger.error("Evaluation failed for project %s: %s", project_id, e)
            log_autonomy_decision(
                _get_structured_logger(),
                "evaluation_failed",
                project_id,
                error_type=type(e).__name__,
                error_message=str(e),
            )
            return None

        if not evaluation.get("all_met"):
            return None

        # Generate conclusion
        aim = project.get("aim", "The project")
        reasoning = evaluation.get("reasoning", "")
        conclusion = f"{aim} has been successfully completed.\n\n{reasoning}"

        now = utcnow().isoformat()
        await self.db.execute(
            """
            UPDATE projects
            SET state = ?, closed_at = ?, conclusion = ?, updated_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (ProjectState.CLOSED.value, now, conclusion, now, project_id),
        )

        await self._add_journal_entry(
            project_id,
            JournalEntryType.MILESTONE,
            f"Project auto-completed based on evaluation.\n\n{reasoning}",
            {"evaluation": evaluation},
        )

        log_autonomy_decision(
            _get_structured_logger(),
            "project_auto_completed",
            project_id,
            met_criteria_count=len(evaluation.get("met_criteria", [])),
        )

        await self._sync_notifications(project_id)
        await NotificationService(self.db).create_project_result_notification(
            project_id,
            conclusion=conclusion,
        )
        return await self._build_project_response(await self._get_project_row(project_id))

    async def _get_project_ids_for_task(self, task_id: str) -> list[str]:
        """Get all project IDs linked to a task."""
        rows = await self.db.fetch_all(
            """
            SELECT pt.project_id
            FROM project_tasks AS pt
            INNER JOIN projects AS p ON p.id = pt.project_id
            WHERE pt.task_id = ? AND p.deleted_at IS NULL
            ORDER BY pt.project_id
            """,
            (task_id,),
        )
        return [row["project_id"] for row in rows]

    async def _get_project_row(self, project_id: str) -> dict[str, Any] | None:
        """Get a project row by ID."""
        return await self.db.fetch_one(
            "SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_id,),
        )

    def _parse_plan(self, plan_json: str | None) -> list[PlanStep]:
        """Parse plan JSON into list of PlanStep objects."""
        if not plan_json:
            return []
        try:
            data = json.loads(plan_json)
            if isinstance(data, list):
                return [PlanStep.model_validate(step) for step in data]
        except (json.JSONDecodeError, Exception):
            pass
        return []

    def _parse_success_criteria(self, criteria_json: str | None) -> list[SuccessCriterion]:
        """Parse success criteria JSON into list of SuccessCriterion objects."""
        if not criteria_json:
            return []
        try:
            data = json.loads(criteria_json)
            if isinstance(data, list):
                return [SuccessCriterion.model_validate(c) for c in data]
        except (json.JSONDecodeError, Exception):
            pass
        return []

    async def cleanup_old_plan_tasks(self, project_id: str) -> int:
        """Remove deprecated auto-created tasks from a previous plan before re-planning.

        Only soft-deletes tasks that were explicitly deprecated by reasoning (e.g. during
        spec revision). Tasks whose output is still relevant are preserved.
        Returns count of tasks cleaned up.
        """
        rows = await self.db.fetch_all(
            """
            SELECT t.id
            FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ?
              AND t.deleted_at IS NULL
              AND t.metadata LIKE '%auto_created_by_project%'
              AND t.status = 'deprecated'
            """,
            (project_id,),
        )
        if not rows:
            return 0

        task_ids = [row["id"] for row in rows]
        now = utcnow().isoformat()
        for task_id in task_ids:
            await self.db.execute(
                "UPDATE tasks SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (now, now, task_id),
            )

        await self._add_journal_entry(
            project_id,
            JournalEntryType.DECISION,
            f"Cleaned up {len(task_ids)} deprecated tasks from previous plan.",
            {"replan_reason": "new plan approved"},
        )
        return len(task_ids)

    async def _auto_start_task(self, task_id: str, project_id: str) -> None:
        """Start a task immediately after creation."""
        project = await self._get_project_row(project_id)
        if not project:
            return
        # Skip if task already completed (e.g. subagent finished during creation)
        task_row = await self.db.fetch_one("SELECT status FROM tasks WHERE id = ?", (task_id,))
        if task_row and task_row["status"] == TaskStatus.COMPLETED.value:
            return
        try:
            await self.task_service.start_task(str(task_id))
        except Exception:
            logger.warning(
                "Auto-start failed for task %s in project %s",
                task_id,
                project_id,
                exc_info=True,
            )

    async def decide_next_action(
        self,
        project_id: str,
        completed_task_id: str,
        completed_task_title: str,
        result_summary: str | None = None,
        *,
        resumed_from_block: bool = False,
    ) -> None:
        """Dispatch a next-action prompt to OpenClaw (fire-and-forget).

        Instead of waiting for the LLM to respond synchronously, this generates
        a one-time password, builds a prompt with full project context, and
        dispatches it via the notification pipeline. The agent responds
        asynchronously via `cyborg project decide-next`.
        """
        import secrets
        from cyborg_server.structured_logging import log_autonomy_decision
        from cyborg_server.services.context_builder import ContextBuilder, ContextScope

        project = await self._get_project_row(project_id)
        if not project or project["state"] != ProjectState.ACTIVE.value:
            return

        # Cycle prevention: check reasoning cycle count
        project_metadata = json_loads(project.get("metadata"), {})
        cycle_count = int(project_metadata.get("reasoning_cycle_count", 0)) + 1
        max_cycles = 15

        if cycle_count > max_cycles:
            logger.warning(
                "Project %s exceeded max reasoning cycles (%d), blocking",
                project_id, max_cycles,
            )
            await self._block_project_cycle_limit(project_id, cycle_count)
            return

        # Generate OTP and store on project
        otp = secrets.token_urlsafe(24)
        now = utcnow()
        await self.db.execute(
            "UPDATE projects SET reasoning_otp = ?, reasoning_otp_created_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (otp, now.isoformat(), now.isoformat(), project_id),
        )

        # Build context for the prompt
        context_builder = ContextBuilder(self.db)
        context = await context_builder.build_project_context(
            project_id=project_id,
            scope=ContextScope.STANDARD,
            focus_reasoning="next_action",
        )

        prompt = self._build_next_action_prompt(context, completed_task_id, project_id, otp, resumed_from_block=resumed_from_block)

        # Dispatch via notification service (fire-and-forget)
        try:
            from cyborg_server.services.notification_service import NotificationService
            notification_service = NotificationService(self.db)
            await notification_service.create_next_action_notification(
                project_id,
                prompt,
                otp,
                completed_task_id,
                now=now,
            )
        except Exception:
            logger.warning(
                "Failed to dispatch next-action prompt for project %s",
                project_id,
                exc_info=True,
            )

        log_autonomy_decision(
            _get_structured_logger(),
            "next_action_dispatched",
            project_id,
            trigger_task_id=completed_task_id,
            cycle_count=cycle_count,
        )

        await self._add_journal_entry(
            project_id,
            JournalEntryType.DECISION,
            f"Dispatched next-action prompt (cycle {cycle_count}).",
            {"cycle_count": cycle_count, "completed_task_id": completed_task_id},
        )

    def _build_next_action_prompt(
        self,
        context: dict[str, Any],
        completed_task_id: str,
        project_id: str,
        otp: str,
        *,
        resumed_from_block: bool = False,
    ) -> str:
        """Build the next-action prompt with context and OTP-secured CLI instructions."""
        core = context["core"]
        project = core["project"]
        criteria = core["success_criteria"].get("criteria", [])
        plan_steps = core["success_criteria"].get("plan_steps", [])
        tasks = context["tasks"]
        all_tasks = tasks.get("all_tasks", [])
        journal = context["journal"].get("entries", [])[-10:]

        # Find the completed task in the context
        completed_task_info = ""
        for t in all_tasks:
            if t.get("id") == completed_task_id:
                completed_task_info = f"Title: {t.get('title', 'Unknown')}\nStatus: {t.get('status', 'unknown')}"
                if t.get("result"):
                    completed_task_info += f"\nResult: {t['result'][:500]}"
                break
        if not completed_task_info:
            completed_task_info = f"Task ID: {completed_task_id} (details not available)"

        # Extract user response from journal when resuming from a block
        user_response_text = None
        if resumed_from_block:
            for entry in reversed(journal):
                entry_meta = json_loads(entry.get("metadata"), {})
                if entry_meta.get("user_response"):
                    user_response_text = entry_meta["user_response"]
                    break
                if "User response to block:" in entry.get("content", ""):
                    content = entry["content"]
                    user_response_text = content.split("User response to block:", 1)[-1].strip()
                    break

        if resumed_from_block:
            parts = [
                "You are managing an autonomous project that was previously blocked waiting for user input.",
                "The user has now approved/resumed the project. Decide what should happen next.",
                "",
                "**IMPORTANT:** The user chose to approve the block (not reject it), meaning they want",
                "the project to continue. Do NOT block the project again unless there is a genuinely",
                "new reason that did not exist before. Prefer create_task or close_project.",
            ]
            if user_response_text:
                parts.extend([
                    "",
                    "## User's Response",
                    user_response_text,
                ])
            parts.extend([
                "",
                "## Project",
                f"Title: {project['title']}",
                f"Aim: {project.get('aim', 'N/A')}",
            ])
        else:
            parts = [
                "You are managing an autonomous project. A task has just completed.",
                "Decide what should happen next based on the project's aim, plan, and success criteria.",
                "",
                "## Project",
                f"Title: {project['title']}",
                f"Aim: {project.get('aim', 'N/A')}",
            ]

        if project.get("method"):
            parts.append(f"Method: {project['method']}")

        if plan_steps:
            parts.extend(["", "## Plan (Reference — use as guidance, not rigid script)"])
            for i, step in enumerate(plan_steps):
                title = step.get("title", f"Step {i + 1}")
                parts.append(f"  {i + 1}. {title}")

        if criteria:
            parts.extend(["", "## Success Criteria"])
            for i, c in enumerate(criteria, 1):
                parts.append(f"  {i}. {c.get('description', '')}")

        parts.extend(["", "## Just Completed", completed_task_info])

        completed_list = [t for t in all_tasks if t.get("status") == "completed"]
        if completed_list:
            parts.extend(["", "## All Completed Tasks"])
            for t in completed_list:
                result_bit = f": {t['result'][:100]}" if t.get("result") else ""
                parts.append(f"  - {t.get('title', 'Unknown')}{result_bit}")

        open_tasks = [t for t in all_tasks if t.get("status") in ("pending", "active", "blocked")]
        if open_tasks:
            parts.extend(["", "## Current Open Tasks"])
            for t in open_tasks:
                parts.append(f"  - [{t.get('status', '?')}] {t.get('title', 'Unknown')}")

        if journal:
            parts.extend(["", "## Recent Activity"])
            for entry in journal[-5:]:
                content = entry.get("content", "")
                entry_meta = json_loads(entry.get("metadata"), {})
                # Preserve full content for user responses and decisions
                if entry_meta.get("user_response") or entry.get("entry_type") == "decision":
                    pass  # keep full content
                elif len(content) > 200:
                    content = content[:200] + "..."
                parts.append(f"  - [{entry.get('entry_type', '?')}] {content}")

        parts.extend([
            "",
            "Based on the above, decide the single best next action:",
            "",
            "1. **create_task** — The project needs another task to progress toward its aim.",
            "2. **close_project** — All success criteria appear to be met. The project is done.",
            "3. **block_project** — The project needs human input before it can continue.",
            "",
            "## Your Action",
            "After deciding, submit your decision using the cyborg CLI with the one-time password below.",
            "This is the ONLY way to submit your decision — do not reply with the decision as text.",
            "",
            f"**One-time password:** `{otp}`",
            f"**Project ID:** `{project_id}`",
            "",
        ])

        parts.extend([
            "### Option 1: Create a new task",
            f"  cyborg project decide-next {project_id} --otp {otp} --action create_task \\",
            '    --task-title "Task title" \\',
            '    --task-description "What the task should do" \\',
            '    --task-plan "Objective: ...\\nExecution: ...\\nSuccess criteria: ..." \\',
            "    --reasoning \"Why this task is needed\" \\",
            "    --task-priority high",
            "",
            "### Option 2: Close the project",
            f"  cyborg project decide-next {project_id} --otp {otp} --action close_project \\",
            '    --reasoning "Why all criteria are met"',
            "",
            "### Option 3: Block the project",
            f"  cyborg project decide-next {project_id} --otp {otp} --action block_project \\",
            '    --block-reason "Why blocked" \\',
            '    --resume-instructions "What needs to happen to unblock" \\',
            '    --reasoning "Why human input is needed"',
            "",
            "You can also use the HTTP API:",
            f"  POST /api/v1/projects/{project_id}/decide-next",
            "  {",
            f'    "otp": "{otp}",',
            '    "action": "create_task|close_project|block_project",',
            '    "reasoning": "...",',
            '    "task_title": "...",  // for create_task',
            '    "task_description": "...",  // for create_task',
            '    "task_plan": "...",  // for create_task',
            '    "task_priority": "high|medium|low",  // for create_task',
            '    "block_reason": "...",  // for block_project',
            '    "resume_instructions": "..."  // for block_project',
            "  }",
        ])

        return "\n".join(parts)

    @staticmethod
    def _build_project_task_session_key(project_id: str, task_short_id: str) -> str:
        from cyborg_server.services.project_service import short_task_id
        return f"cyborg:project:{short_task_id(project_id)}:task:{task_short_id}"

    async def _create_reasoned_task(
        self,
        project_id: str,
        task_definition: dict[str, Any],
        reasoning: str,
    ) -> None:
        """Create a task based on reasoning decision."""
        title = task_definition.get("title", "Next step")[:200]
        description = task_definition.get("description", "")
        plan = task_definition.get("plan", "")
        priority_str = task_definition.get("priority", "high")

        try:
            priority = TaskPriority(priority_str)
        except ValueError:
            priority = TaskPriority.HIGH

        # Pre-generate task short ID for session key (notification fires during create_task)
        from uuid import uuid4
        from cyborg_server.services.project_service import short_task_id
        task_short_id = short_task_id(str(uuid4()))

        # Resolve output directory
        output_directory: str | None = None
        try:
            from cyborg_server.services.project_service import ProjectService
            project_service = ProjectService(self.db)
            project_path = await project_service.get_project_path(project_id)
            output_directory = str(project_path / "tasks" / "pending")
        except Exception:
            pass

        if output_directory and not plan.endswith(output_directory):
            plan += (
                f"\n\n## Output Directory\n"
                f"All task artifacts must be written to: `{output_directory}`\n"
                f"- Use descriptive filenames for each artifact.\n"
                f"- Put the primary result in `RESULT.md`.\n"
                f"- Register all output files via the task files API."
            )

        session_key = self._build_project_task_session_key(project_id, task_short_id)
        task_payload = TaskCreate(
            title=title,
            description=description,
            plan=plan,
            priority=priority,
            project_ids=[project_id],
            metadata={
                "auto_created_by_project": True,
                "source": "reasoning",
                "target_session": {"session_key": session_key},
            },
        )

        task = await self.task_service.create_task(task_payload)
        await self._auto_start_task(str(task.id), project_id)

        # Update output directory with actual task ID
        if output_directory and output_directory.endswith("/pending"):
            try:
                from cyborg_server.services.project_service import short_task_id
                from cyborg_server.models import TaskUpdate

                real_dir = output_directory.replace("/pending", f"/{short_task_id(str(task.id))}")
                updated_plan = plan.replace(output_directory, real_dir)
                await self.task_service.update_task(str(task.id), TaskUpdate(plan=updated_plan))
            except Exception:
                pass

        await self._add_journal_entry(
            project_id,
            JournalEntryType.MILESTONE,
            f"Reasoning created task: {title}",
            {"reasoning": reasoning[:200]},
        )

    async def _create_initial_task(self, project_id: str, step: PlanStep) -> None:
        """Create the initial task from plan step 0 (deterministic first task)."""
        # Pre-generate task short ID for session key (notification fires during create_task)
        from uuid import uuid4
        from cyborg_server.services.project_service import short_task_id
        task_short_id = short_task_id(str(uuid4()))

        # Resolve output directory
        output_directory: str | None = None
        try:
            from cyborg_server.services.project_service import ProjectService
            project_service = ProjectService(self.db)
            project_path = await project_service.get_project_path(project_id)
            output_directory = str(project_path / "tasks" / "pending")
        except Exception:
            pass

        plan = (
            f"Objective: {step.title}\n"
            f"Execution: {step.description}\n"
            f"Success criteria: {step.criteria}"
        )
        if output_directory:
            plan += (
                f"\n\n## Output Directory\n"
                f"All task artifacts must be written to: `{output_directory}`\n"
                f"- Use descriptive filenames for each artifact.\n"
                f"- Put the primary result in `RESULT.md`.\n"
                f"- Register all output files via the task files API."
            )

        session_key = self._build_project_task_session_key(project_id, task_short_id)
        task_payload = TaskCreate(
            title=step.title,
            description=step.description,
            plan=plan,
            priority=TaskPriority.HIGH,
            project_ids=[project_id],
            metadata={
                "auto_created_by_project": True,
                "source": "initial_plan_step",
                "project_step_index": 0,
                "target_session": {"session_key": session_key},
            },
        )

        task = await self.task_service.create_task(task_payload)
        await self._auto_start_task(str(task.id), project_id)

        # Update output directory with actual task ID
        if output_directory and output_directory.endswith("/pending"):
            try:
                from cyborg_server.services.project_service import short_task_id
                from cyborg_server.models import TaskUpdate

                real_dir = output_directory.replace("/pending", f"/{short_task_id(str(task.id))}")
                updated_plan = plan.replace(output_directory, real_dir)
                await self.task_service.update_task(str(task.id), TaskUpdate(plan=updated_plan))
            except Exception:
                pass

        await self._add_journal_entry(
            project_id,
            JournalEntryType.MILESTONE,
            f"Auto-created initial task: {step.title}",
            {"step_title": step.title},
        )

    async def _close_project_from_reasoning(
        self,
        project_id: str,
        reasoning: str,
        decision: dict[str, Any],
    ) -> None:
        """Close a project based on reasoning decision."""
        project = await self._get_project_row(project_id)
        if not project:
            return

        aim = project.get("aim", "The project")
        conclusion = f"{aim} has been successfully completed.\n\n{reasoning}"

        now = utcnow().isoformat()
        await self.db.execute(
            """
            UPDATE projects
            SET state = ?, closed_at = ?, conclusion = ?, updated_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (ProjectState.CLOSED.value, now, conclusion, now, project_id),
        )

        await self._add_journal_entry(
            project_id,
            JournalEntryType.MILESTONE,
            f"Project auto-completed by reasoning.\n\n{reasoning}",
            {"decision": decision},
        )

        await self._sync_notifications(project_id)
        await NotificationService(self.db).create_project_result_notification(
            project_id,
            conclusion=conclusion,
        )

    async def _block_project_from_reasoning(
        self,
        project_id: str,
        block_reason: str,
        resume_instructions: str,
        reasoning: str,
    ) -> None:
        """Block a project based on reasoning decision."""
        project = await self._get_project_row(project_id)
        project_title = project["title"] if project else "Unknown"

        now = utcnow().isoformat()
        await self.db.execute(
            """
            UPDATE projects
            SET state = ?, blocked_reason = ?, blocked_resume_instructions = ?, updated_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (ProjectState.PAUSED.value, block_reason, resume_instructions, now, project_id),
        )

        # Create a task_input approval with text input so the user can respond
        input_schema = json_dumps({"type": "text", "prompt": "How should the project proceed?"})
        proposal_data = json_dumps({
            "project_id": project_id,
            "project_title": project_title,
            "reason": block_reason,
            "resume_instructions": resume_instructions,
        })
        await self.db.execute(
            """
            INSERT INTO approvals (
                id, approval_type, entity_id, title, description,
                proposal_data, input_schema, status, priority, requested_at, requested_by,
                metadata, created_at
            ) VALUES (?, 'task_input', ?, ?, ?, ?, ?, 'pending', 'high', ?, 'system', ?, ?)
            """,
            (
                str(uuid4()),
                project_id,
                f"Project blocked: {project_title}",
                block_reason,
                proposal_data,
                input_schema,
                now,
                json_dumps({"entity_kind": "project", "resume_instructions": resume_instructions}),
                now,
            ),
        )

        await self._add_journal_entry(
            project_id,
            JournalEntryType.DECISION,
            f"Project blocked by reasoning: {block_reason}",
            {"block_reason": block_reason, "resume_instructions": resume_instructions, "reasoning": reasoning[:300]},
        )

        await self._sync_notifications(project_id)

    async def _block_project_cycle_limit(self, project_id: str, cycle_count: int) -> None:
        """Block a project that exceeded the reasoning cycle limit."""
        project = await self._get_project_row(project_id)
        project_title = project["title"] if project else "Unknown"

        now = utcnow().isoformat()
        block_reason = f"Project exceeded maximum reasoning cycles ({cycle_count}). Manual review required."
        await self.db.execute(
            """
            UPDATE projects
            SET state = ?, blocked_reason = ?, updated_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (ProjectState.PAUSED.value, block_reason, now, project_id),
        )

        # Create a task_input approval with text input so the user can respond
        input_schema = json_dumps({"type": "text", "prompt": "The project hit the reasoning cycle limit. How should it proceed?"})
        proposal_data = json_dumps({
            "project_id": project_id,
            "project_title": project_title,
            "reason": block_reason,
            "cycle_count": cycle_count,
        })
        await self.db.execute(
            """
            INSERT INTO approvals (
                id, approval_type, entity_id, title, description,
                proposal_data, input_schema, status, priority, requested_at, requested_by,
                metadata, created_at
            ) VALUES (?, 'task_input', ?, ?, ?, ?, ?, 'pending', 'high', ?, 'system', ?, ?)
            """,
            (
                str(uuid4()),
                project_id,
                f"Cycle limit reached: {project_title}",
                block_reason,
                proposal_data,
                input_schema,
                now,
                json_dumps({"entity_kind": "project"}),
                now,
            ),
        )

        await self._add_journal_entry(
            project_id,
            JournalEntryType.DECISION,
            block_reason,
            {"cycle_count": cycle_count},
        )

        await self._sync_notifications(project_id)

    async def _add_journal_entry(
        self,
        project_id: str,
        entry_type: JournalEntryType,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a journal entry to a project."""
        entry_id = str(uuid4())
        now = utcnow().isoformat()
        await self.db.execute(
            """
            INSERT INTO project_journal_entries (id, project_id, entry_type, content, created_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (entry_id, project_id, entry_type.value, content, now, json_dumps(metadata or {})),
        )

    async def _build_project_response(self, row: dict[str, Any] | None) -> ProjectResponse:
        """Build a ProjectResponse from a database row."""
        if not row:
            raise NotFoundError("Project not found")

        # Parse JSON fields
        row.pop("auto_execute", None)
        row.pop("reasoning_otp", None)
        row.pop("reasoning_otp_created_at", None)
        row["plan"] = self._parse_plan(row.get("plan"))
        row["success_criteria"] = self._parse_success_criteria(row.get("success_criteria"))
        row["metadata"] = json_loads(row.get("metadata"), {})
        row = await self.project_spec_service.populate_project_spec_fields(row)
        
        # Get task IDs
        task_ids = await self.db.fetch_all(
            """
            SELECT pt.task_id
            FROM project_tasks AS pt
            INNER JOIN tasks AS t ON t.id = pt.task_id
            WHERE pt.project_id = ? AND t.deleted_at IS NULL
            ORDER BY pt.task_id
            """,
            (row["id"],),
        )
        row["task_ids"] = [t["task_id"] for t in task_ids]

        return ProjectResponse.model_validate(row)

    async def block_project(self, project_id: str, reason: str, resume_instructions: str | None = None) -> ProjectResponse:
        """Block a project waiting for user input or external action.
        
        This triggers a webhook notification to OpenClaw.
        """
        project = await self._get_project_row(project_id)
        if not project:
            raise NotFoundError(f"Project '{project_id}' was not found")
        
        if project["state"] not in (ProjectState.ACTIVE.value, ProjectState.PLANNING.value):
            raise ConflictError(f"Cannot block project in state '{project['state']}'")
        
        now = utcnow().isoformat()
        
        # Update project state to paused (blocked)
        await self.db.execute(
            """
            UPDATE projects
            SET state = ?, paused_at = ?, blocked_reason = ?, blocked_resume_instructions = ?, updated_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (ProjectState.PAUSED.value, now, reason, resume_instructions, now, project_id),
        )
        
        # Add journal entry
        await self._add_journal_entry(
            project_id,
            JournalEntryType.BLOCKER,
            f"Project blocked: {reason}",
            {"resume_instructions": resume_instructions} if resume_instructions else {},
        )

        # Create an approval record so the block shows in the dashboard
        from uuid import uuid4

        proposal_data = {
            "project_id": project_id,
            "project_title": project["title"],
            "reason": reason,
            "resume_instructions": resume_instructions,
        }
        input_schema = json_dumps({"type": "text", "prompt": "How should the project proceed?"})
        await self.db.execute(
            """
            INSERT INTO approvals (
                id, approval_type, entity_id, title, description,
                proposal_data, input_schema, status, priority, requested_at, requested_by,
                metadata, created_at
            ) VALUES (?, 'task_input', ?, ?, ?, ?, ?, 'pending', 'high', ?, 'system', ?, ?)
            """,
            (
                str(uuid4()),
                project_id,
                f"Project blocked: {project['title']}",
                reason,
                json_dumps(proposal_data),
                input_schema,
                now,
                json_dumps({"entity_kind": "project", "resume_instructions": resume_instructions}),
                now,
            ),
        )
        
        # Trigger webhook notification
        await self._trigger_project_webhook(
            event=WebhookEvent.PROJECT_BLOCKED,
            project_id=project_id,
            project_title=project["title"],
            reason=reason,
            resume_instructions=resume_instructions,
        )
        await self._sync_notifications(project_id)
        return await self._build_project_response(await self._get_project_row(project_id))

    async def mark_ready_for_review(self, project_id: str, review_notes: str | None = None) -> ProjectResponse:
        """Mark a project as ready for review.
        
        This triggers a webhook notification to OpenClaw.
        """
        project = await self._get_project_row(project_id)
        if not project:
            raise NotFoundError(f"Project '{project_id}' was not found")
        
        if project["state"] != ProjectState.ACTIVE.value:
            raise ConflictError(f"Cannot mark project for review in state '{project['state']}'")
        
        now = utcnow().isoformat()
        
        # Add journal entry
        content = "Project ready for review"
        if review_notes:
            content += f"\n\nNotes: {review_notes}"
        
        await self._add_journal_entry(
            project_id,
            JournalEntryType.MILESTONE,
            content,
            {"ready_for_review": True},
        )
        
        # Trigger webhook notification
        await self._trigger_project_webhook(
            event=WebhookEvent.PROJECT_READY_FOR_REVIEW,
            project_id=project_id,
            project_title=project["title"],
            review_notes=review_notes,
        )
        
        return await self._build_project_response(await self._get_project_row(project_id))

    async def _trigger_project_webhook(
        self,
        event: str,
        project_id: str,
        project_title: str,
        reason: str | None = None,
        resume_instructions: str | None = None,
        review_notes: str | None = None,
    ) -> None:
        """Trigger webhook notification for project events."""
        webhook_service = self._get_webhook_service()
        if webhook_service is None:
            return
        
        # Get project metadata for session_key
        project = await self._get_project_row(project_id)
        metadata = {}
        session_key = None
        if project and project.get("metadata"):
            try:
                metadata = json.loads(project["metadata"])
                session_key = metadata.get("session_key")
            except (json.JSONDecodeError, Exception):
                pass
        
        try:
            await webhook_service.trigger_event(
                event=event,
                project_id=project_id,
                task_title=project_title,
                result_summary=reason or review_notes,
                session_key=session_key,
                metadata={
                    **metadata,
                    "resume_instructions": resume_instructions,
                    "review_notes": review_notes,
                },
            )
        except Exception:
            # Don't let webhook failures affect project operations
            pass

    # ------------------------------------------------------------------
    # Doctor: diagnose and fix stuck projects
    # ------------------------------------------------------------------

    async def diagnose(self) -> list[dict[str, Any]]:
        """Scan for common project health problems.

        Detects:
        - active projects with an approved spec but zero tasks
        - blocked tasks without a pending approval record
        """
        problems: list[dict[str, Any]] = []

        rows = await self.db.fetch_all(
            """
            SELECT p.id, p.title
            FROM projects p
            LEFT JOIN project_tasks pt ON pt.project_id = p.id
            WHERE p.state = 'active'
              AND p.current_spec_id IS NOT NULL
              AND p.deleted_at IS NULL
            GROUP BY p.id
            HAVING COUNT(pt.task_id) = 0
            """,
        )
        for row in rows:
            problems.append({"project_id": row["id"], "title": row["title"], "problem": "active_with_no_tasks"})

        blocked_rows = await self.db.fetch_all(
            """
            SELECT t.id as task_id, t.title, t.blocked_reason, pt.project_id, p.title as project_title
            FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            INNER JOIN projects p ON p.id = pt.project_id
            WHERE t.status = 'blocked' AND t.deleted_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.entity_id = t.id AND a.status = 'pending')
            """,
        )
        for row in blocked_rows:
            problems.append({
                "task_id": row["task_id"],
                "title": row["title"],
                "project_id": row["project_id"],
                "project_title": row["project_title"],
                "blocked_reason": row["blocked_reason"],
                "problem": "blocked_task_without_approval",
            })

        # Obsolete approvals: pending approvals whose entity can't accept them
        obsolete_rows = await self.db.fetch_all(
            """
            SELECT a.id as approval_id, a.approval_type, a.title, a.entity_id,
                   a.metadata, a.requested_at,
                   p.id as project_id, p.title as project_title, p.state as project_state,
                   p.blocked_reason as project_blocked_reason
            FROM approvals a
            LEFT JOIN projects p ON p.id = a.entity_id AND p.deleted_at IS NULL
            LEFT JOIN tasks t ON t.id = a.entity_id AND t.deleted_at IS NULL
            WHERE a.status = 'pending'
              AND (
                (a.approval_type = 'project_plan' AND (p.state = 'closed' OR p.id IS NULL))
                OR (a.approval_type = 'task_input'
                    AND json_extract(a.metadata, '$.entity_kind') = 'project'
                    AND (p.state = 'closed' OR p.id IS NULL OR p.blocked_reason IS NULL))
                OR (a.approval_type = 'task_input'
                    AND (json_extract(a.metadata, '$.entity_kind') IS NULL
                         OR json_extract(a.metadata, '$.entity_kind') != 'project')
                    AND (t.id IS NULL OR t.status != 'blocked'))
              )
            """,
        )
        for row in obsolete_rows:
            problems.append({
                "approval_id": row["approval_id"],
                "approval_type": row["approval_type"],
                "title": row["title"],
                "entity_id": row["entity_id"],
                "project_id": row["project_id"],
                "project_title": row["project_title"],
                "project_state": row["project_state"],
                "problem": "obsolete_approval",
            })

        # Duplicate pending approvals: more than one pending approval of the
        # same type for the same entity.
        duplicate_rows = await self.db.fetch_all(
            """
            SELECT a.approval_type, a.entity_id, COUNT(*) AS cnt,
                   MIN(a.id) AS keep_id,
                   GROUP_CONCAT(a.id) AS approval_ids,
                   GROUP_CONCAT(a.title, ' | ') AS titles
            FROM approvals a
            WHERE a.status = 'pending'
            GROUP BY a.approval_type, a.entity_id
            HAVING cnt > 1
            """,
        )
        for row in duplicate_rows:
            approval_ids = row["approval_ids"].split(",")
            keep_id = row["keep_id"]
            cancel_ids = [aid for aid in approval_ids if aid != keep_id]
            problems.append({
                "approval_type": row["approval_type"],
                "entity_id": row["entity_id"],
                "title": row["titles"],
                "problem": "duplicate_pending_approvals",
                "keep_approval_id": keep_id,
                "cancel_approval_ids": cancel_ids,
            })

        # Failed next_action notifications blocking project progress
        failed_next_action_rows = await self.db.fetch_all(
            """
            SELECT n.id AS notification_id, n.entity_id AS project_id, n.last_delivery_error,
                   p.title, n.created_at
            FROM notifications n
            INNER JOIN projects p ON p.id = n.entity_id AND p.deleted_at IS NULL
            WHERE n.notification_type = 'next_action'
              AND n.status = 'pending'
              AND n.delivery_status = 'failed'
              AND p.state = 'active'
            """,
        )
        for row in failed_next_action_rows:
            problems.append({
                "project_id": row["project_id"],
                "notification_id": row["notification_id"],
                "title": row["title"],
                "last_delivery_error": row["last_delivery_error"],
                "notification_created_at": row["created_at"],
                "problem": "failed_next_action_notification",
            })

        return problems

    async def bootstrap_stuck_project(self, project_id: str) -> dict[str, Any]:
        """Bootstrap a stuck project by creating the first task from plan step 0.

        Only works for active projects with zero tasks.
        """
        project = await self._get_project_row(project_id)
        if not project or project["state"] != ProjectState.ACTIVE.value:
            return {"project_id": project_id, "action": "skipped", "reason": "not active"}

        # Confirm zero tasks
        task_count = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM project_tasks WHERE project_id = ?",
            (project_id,),
        )
        if task_count and task_count["cnt"] > 0:
            return {"project_id": project_id, "action": "skipped", "reason": "has tasks"}

        # Create the first task deterministically from plan step 0
        plan = self._parse_plan(project.get("plan"))
        if not plan:
            return {"project_id": project_id, "action": "skipped", "reason": "no plan"}

        await self._create_initial_task(project_id, plan[0])

        await self._add_journal_entry(
            project_id,
            JournalEntryType.DECISION,
            "Doctor bootstrapped stuck project — created first task from plan step 0",
            {"source": "doctor", "problem": "active_with_no_tasks"},
        )

        return {"project_id": project_id, "action": "bootstrapped"}

    async def redrive_next_action(self, project_id: str) -> dict[str, Any]:
        """Cancel a failed next_action notification and re-dispatch decide_next_action."""
        from cyborg_server.services.notification_service import NotificationService

        # Cancel the failed notification
        await self.db.execute(
            "UPDATE notifications SET status = 'resolved', resolved_at = ?, updated_at = ? WHERE entity_id = ? AND notification_type = 'next_action' AND status = 'pending' AND delivery_status = 'failed'",
            (utcnow().isoformat(), utcnow().isoformat(), project_id),
        )

        # Find the completed task that triggered the original next_action
        last_completed = await self.db.fetch_one(
            """
            SELECT t.id, t.title FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.status = 'completed' AND t.deleted_at IS NULL
            ORDER BY t.updated_at DESC LIMIT 1
            """,
            (project_id,),
        )
        completed_task_id = last_completed["id"] if last_completed else None
        completed_task_title = last_completed["title"] if last_completed else None

        await self.decide_next_action(
            project_id,
            completed_task_id=completed_task_id or "",
            completed_task_title=completed_task_title or "",
        )

        return {"project_id": project_id, "action": "redriven_next_action"}

    async def verify_decide_next(self, project_id: str, payload: Any) -> ProjectResponse:
        """Process an async next-action response from reasoning.

        Validates the OTP, clears it (one-time use), then executes the
        chosen action (create_task, close_project, or block_project).
        """
        from cyborg_server.exceptions import ConflictError
        from cyborg_server.models import ProjectDecideNextRequest

        req = ProjectDecideNextRequest.model_validate(payload)
        project = await self._get_project_row(project_id)
        if not project:
            raise ConflictError(f"Project '{project_id}' not found")
        if project["state"] != ProjectState.ACTIVE.value:
            raise ConflictError(f"Project is '{project['state']}', expected 'active'")

        # Validate OTP
        stored_otp = project.get("reasoning_otp")
        if not stored_otp or req.otp != stored_otp:
            raise ConflictError("Invalid or expired reasoning OTP")

        # Clear OTP (one-time use)
        now = utcnow().isoformat()
        await self.db.execute(
            "UPDATE projects SET reasoning_otp = NULL, reasoning_otp_created_at = NULL, updated_at = ? WHERE id = ?",
            (now, project_id),
        )

        action = req.action
        reasoning = req.reasoning or ""

        # Increment cycle count
        project_metadata = json_loads(project.get("metadata"), {})
        cycle_count = int(project_metadata.get("reasoning_cycle_count", 0)) + 1
        project_metadata["reasoning_cycle_count"] = cycle_count
        await self.db.execute(
            "UPDATE projects SET metadata = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (json.dumps(project_metadata), now, project_id),
        )

        if action == "create_task":
            task_def = {
                "title": req.task_title or "Next step",
                "description": req.task_description or "",
                "plan": req.task_plan or "",
                "priority": req.task_priority or "high",
            }
            await self._create_reasoned_task(project_id, task_def, reasoning)
        elif action == "close_project":
            await self._close_project_from_reasoning(project_id, reasoning, {"action": action})
        elif action == "block_project":
            await self._block_project_from_reasoning(
                project_id,
                req.block_reason or "Project blocked by reasoning",
                req.resume_instructions or "",
                reasoning,
            )
        else:
            raise ConflictError(f"Unknown action: {action}")

        await self._add_journal_entry(
            project_id,
            JournalEntryType.DECISION,
            f"Reasoning decision (async): {action}. {reasoning[:300]}",
            {"action": action, "reasoning": reasoning, "cycle_count": cycle_count},
        )

        return await self._build_project_response(await self._get_project_row(project_id))

    async def create_missing_approval(self, task_id: str) -> dict[str, Any]:
        """Create a task_input approval for a blocked task that lacks one."""
        from uuid import uuid4

        row = await self.db.fetch_one(
            "SELECT id, title, blocked_reason, blocked_resume_instructions FROM tasks WHERE id = ? AND status = 'blocked' AND deleted_at IS NULL",
            (task_id,),
        )
        if not row:
            return {"task_id": task_id, "action": "skipped", "reason": "not blocked"}

        # Check if an approval already exists
        existing = await self.db.fetch_one(
            "SELECT id FROM approvals WHERE entity_id = ? AND status = 'pending'",
            (task_id,),
        )
        if existing:
            return {"task_id": task_id, "action": "skipped", "reason": "approval already exists"}

        approval_id = str(uuid4())
        now_iso = utcnow().isoformat()
        reason = row["blocked_reason"] or "Task is blocked"
        proposal_data = json_dumps({
            "task_id": task_id,
            "task_title": row["title"],
            "reason": reason,
            "resume_instructions": row["blocked_resume_instructions"],
        })

        await self.db.execute(
            """
            INSERT INTO approvals (
                id, approval_type, entity_id, title, description,
                proposal_data, status, priority, requested_at, requested_by,
                input_schema, created_at
            ) VALUES (?, 'task_input', ?, ?, ?, ?, 'pending', 'normal', ?, 'doctor', NULL, ?)
            """,
            (
                approval_id,
                task_id,
                f"Task blocked: {row['title']}",
                reason,
                proposal_data,
                now_iso,
                now_iso,
            ),
        )

        return {"task_id": task_id, "action": "approval_created", "approval_id": approval_id}

    async def cancel_obsolete_approval(self, approval_id: str) -> dict[str, Any]:
        """Cancel a pending approval whose target entity can no longer accept it."""
        approval = await self.db.fetch_one(
            "SELECT id, approval_type, entity_id FROM approvals WHERE id = ? AND status = 'pending'",
            (approval_id,),
        )
        if not approval:
            return {"approval_id": approval_id, "action": "skipped", "reason": "not pending"}

        await self.db.execute(
            "UPDATE approvals SET status = 'cancelled', reviewed_at = ?, reviewed_by = 'doctor' WHERE id = ?",
            (utcnow().isoformat(), approval_id),
        )
        return {"approval_id": approval_id, "action": "cancelled"}
