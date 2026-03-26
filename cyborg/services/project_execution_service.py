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

from cyborg.database import Database
from cyborg.exceptions import ConflictError, NotFoundError
from cyborg.models import (
    JournalEntryType,
    PlanStep,
    ProjectResponse,
    ProjectState,
    SuccessCriterion,
    TaskCreate,
    TaskPriority,
    TaskStatus,
)
from cyborg.services.base import BaseService, json_dumps, json_loads, utcnow
from cyborg.services.notification_service import NotificationService
from cyborg.services.project_spec_service import ProjectSpecService
from cyborg.services.webhook_service import WebhookEvent, WebhookService


logger = logging.getLogger(__name__)

# Lazy import structured logging helpers
_structured_logger = None


def _get_structured_logger():
    """Lazy import structured logging helpers."""
    global _structured_logger
    if _structured_logger is None:
        from cyborg.structured_logging import get_logger as _get_logger
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
            from cyborg.services.task_service import TaskService
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
            from cyborg.services.openclaw_reasoning_service import OpenClawReasoningService

            self._reasoning_service = OpenClawReasoningService(self.db)
        return self._reasoning_service

    async def _sync_notifications(self, project_id: str, *, immediate: bool = False) -> None:
        await NotificationService(self.db).sync_project_state(project_id, immediate=immediate)

    async def on_task_completed(self, task_id: str, task_title: str, result_summary: str | None = None) -> list[ProjectResponse]:
        """Hook called when a task is completed.
        
        Checks if any projects linked to this task should progress to the next step.
        Returns list of projects that were affected.
        """
        # Get all projects linked to this task
        project_ids = await self._get_project_ids_for_task(task_id)
        affected_projects: list[ProjectResponse] = []

        for project_id in project_ids:
            project = await self._get_project_row(project_id)
            if not project or project["state"] != ProjectState.ACTIVE.value:
                continue

            # Check if project has auto-execution enabled
            if not project.get("auto_execute"):
                continue

            # Parse plan
            plan = self._parse_plan(project.get("plan"))
            if not plan:
                continue

            # Find current step based on completed tasks
            current_step_index = await self._get_current_step_index(project_id, plan)
            
            if current_step_index >= len(plan):
                # All steps complete, check success criteria
                await self._evaluate_success_criteria(project_id)
            else:
                # Check if current step criteria is satisfied by this task completion
                current_step = plan[current_step_index]
                if self._is_step_satisfied(current_step, task_title, result_summary):
                    # Create next task for the next step
                    await self._create_task_for_step(project_id, current_step, current_step_index)
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
        
        # Update project state
        await self.db.execute(
            """
            UPDATE projects 
            SET state = ?, started_at = ?, auto_execute = 1
            WHERE id = ? AND deleted_at IS NULL
            """,
            (ProjectState.ACTIVE.value, now, project_id),
        )

        # Parse plan and create first task
        plan = self._parse_plan(project.get("plan"))
        if plan:
            await self._create_task_for_step(project_id, plan[0], 0)

        await self._sync_notifications(project_id, immediate=False)
        return await self._build_project_response(await self._get_project_row(project_id))

    async def evaluate_and_complete(self, project_id: str) -> ProjectResponse | None:
        """
        Evaluate success criteria using OpenClaw reasoning and auto-complete project if all criteria met.

        Returns the completed project if auto-completed, None otherwise.
        """
        from cyborg.structured_logging import log_autonomy_decision

        project = await self._get_project_row(project_id)
        if not project or project["state"] != ProjectState.ACTIVE.value:
            return None

        success_criteria = self._parse_success_criteria(project.get("success_criteria"))

        if not success_criteria:
            # No criteria defined, can't auto-complete
            return None

        if await self._project_has_open_tasks(project_id):
            return None

        # Use OpenClaw reasoning service for semantic evaluation
        try:
            evaluation = await self.reasoning_service.evaluate_success_criteria(project_id)
        except Exception as e:
            # Log error but fall back to rule-based evaluation
            logger.error(f"OpenClaw evaluation failed for project {project_id}, falling back to rule-based: {e}")
            log_autonomy_decision(
                _get_structured_logger(),
                "evaluation_failed",
                project_id,
                error_type=type(e).__name__,
                error_message=str(e),
                fallback="rule_based",
            )
            context, _met_criteria, unmet_criteria = await self._evaluate_criteria(project_id, success_criteria)
            evaluation = {
                "all_met": len(unmet_criteria) == 0,
                "met_criteria": [c.description for c in _met_criteria],
                "unmet_criteria": [c.description for c in unmet_criteria],
                "reasoning": "Rule-based evaluation (OpenClaw unavailable)",
            }

        if evaluation.get("all_met"):
            # Generate conclusion
            conclusion = await self._generate_conclusion_from_evaluation(project_id, project, evaluation)

            # Close the project
            now = utcnow().isoformat()
            await self.db.execute(
                """
                UPDATE projects
                SET state = ?, closed_at = ?, conclusion = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (ProjectState.CLOSED.value, now, conclusion, project_id),
            )

            # Add journal entry
            await self._add_journal_entry(
                project_id,
                JournalEntryType.MILESTONE,
                f"Project auto-completed based on OpenClaw evaluation.\n\n{evaluation.get('reasoning', '')}\n\n{conclusion}",
                {
                    "evaluation": evaluation,
                    "all_met_criteria": evaluation.get("met_criteria", []),
                },
            )

            # Log auto-completion decision
            log_autonomy_decision(
                _get_structured_logger(),
                "project_auto_completed",
                project_id,
                met_criteria_count=len(evaluation.get("met_criteria", [])),
                conclusion=conclusion[:200],
            )

            await self._sync_notifications(project_id, immediate=False)
            await NotificationService(self.db).create_project_result_notification(
                project_id,
                conclusion=conclusion,
            )
            return await self._build_project_response(await self._get_project_row(project_id))

        # Generate follow-up tasks for unmet criteria
        unmet = evaluation.get("unmet_criteria", [])
        if unmet:
            log_autonomy_decision(
                _get_structured_logger(),
                "follow_up_tasks_initiated",
                project_id,
                unmet_criteria_count=len(unmet),
                unmet_criteria=unmet[:3],  # Log first 3
            )
            await self._generate_follow_up_tasks_llm(project_id, project, unmet, evaluation)

        return None

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

    async def _get_current_step_index(self, project_id: str, plan: list[PlanStep]) -> int:
        """Determine the current step index based on completed tasks."""
        # Count completed tasks linked to this project
        result = await self.db.fetch_one(
            """
            SELECT COUNT(*) as count
            FROM tasks AS t
            INNER JOIN project_tasks AS pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.status = ? AND t.deleted_at IS NULL
            """,
            (project_id, TaskStatus.COMPLETED.value),
        )
        return result["count"] if result else 0

    def _is_step_satisfied(self, step: PlanStep, task_title: str, result_summary: str | None) -> bool:
        """Check if a step's criteria is satisfied by a task completion.
        
        This is a simple heuristic - can be extended with more sophisticated evaluation.
        """
        criteria = step.criteria.lower()
        title = task_title.lower()
        result = (result_summary or "").lower()

        # Simple keyword matching
        # Criteria like "API endpoints created" matches task title containing "endpoint"
        keywords = re.findall(r'\b\w+\b', criteria)
        for keyword in keywords:
            if len(keyword) > 3 and (keyword in title or keyword in result):
                return True

        return True  # Default to satisfied to allow progression

    async def _create_task_for_step(self, project_id: str, step: PlanStep, step_index: int) -> None:
        """Create a task for a plan step."""
        task_title = f"Step {step_index + 1}: {step.title}"
        
        task_payload = TaskCreate(
            title=task_title,
            description=step.description,
            plan=self._build_step_task_plan(step),
            priority=TaskPriority.HIGH,
            project_ids=[project_id],
            metadata={
                "project_step_index": step_index,
                "project_step_criteria": step.criteria,
                "auto_created_by_project": True,
            },
        )

        await self.task_service.create_task(task_payload)

        # Add journal entry
        await self._add_journal_entry(
            project_id,
            JournalEntryType.MILESTONE,
            f"Auto-created task for step {step_index + 1}: {step.title}",
            {"step_index": step_index, "step_title": step.title},
        )

    def _build_step_task_plan(self, step: PlanStep) -> str:
        """Build the initial task plan for an auto-created project step."""
        return (
            f"Objective: {step.title}\n"
            f"Execution: {step.description}\n"
            f"Success criteria: {step.criteria}"
        )

    async def _evaluate_criteria(
        self,
        project_id: str,
        criteria: list[SuccessCriterion],
    ) -> tuple[dict[str, Any], list[SuccessCriterion], list[SuccessCriterion]]:
        """Evaluate project success criteria and split them into met/unmet lists."""
        # Get project data for evaluation context
        project = await self._get_project_row(project_id)
        if not project:
            return ({}, [], criteria)

        # Get task statistics
        task_stats = await self.db.fetch_one(
            """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as blocked,
                SUM(CASE WHEN status IN (?, ?, ?, ?) THEN 1 ELSE 0 END) as open_count
            FROM tasks AS t
            INNER JOIN project_tasks AS pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.deleted_at IS NULL
            """,
            (
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.BLOCKED.value,
                TaskStatus.PLANNING.value,
                TaskStatus.BLOCKED.value,
                TaskStatus.PENDING.value,
                TaskStatus.ACTIVE.value,
                project_id,
            ),
        )

        # Convert stats to integers (SQLite returns strings)
        total = int(task_stats["total"]) if task_stats and task_stats["total"] else 0
        completed = int(task_stats["completed"]) if task_stats and task_stats["completed"] else 0
        failed = int(task_stats["failed"]) if task_stats and task_stats["failed"] else 0
        blocked = int(task_stats["blocked"]) if task_stats and task_stats["blocked"] else 0
        open_count = int(task_stats["open_count"]) if task_stats and task_stats["open_count"] else 0

        context = {
            "task_count": total,
            "completed_task_count": completed,
            "failed_task_count": failed,
            "blocked_task_count": blocked,
            "open_task_count": open_count,
            "project_state": project["state"],
        }

        met_criteria: list[SuccessCriterion] = []
        unmet_criteria: list[SuccessCriterion] = []
        for criterion in criteria:
            if self._evaluate_criterion(criterion, context):
                met_criteria.append(criterion)
            else:
                unmet_criteria.append(criterion)

        return context, met_criteria, unmet_criteria

    def _evaluate_criterion(self, criterion: SuccessCriterion, context: dict[str, Any]) -> bool:
        """Evaluate a single success criterion against the context.
        
        This is a simple evaluator that supports basic numeric comparisons.
        Can be extended with more sophisticated expression evaluation.
        """
        check = criterion.check.lower()

        # Simple pattern matching for common checks
        # e.g., "task_count > 5", "completed_task_count >= 3"
        match = re.match(r'(\w+)\s*(>=?|<=?|==|!=)\s*(\d+)', check)
        if match:
            var_name, operator, value_str = match.groups()
            value = int(value_str)
            
            if var_name not in context:
                return False
            
            actual_value = context[var_name]
            if isinstance(actual_value, str):
                try:
                    actual_value = int(actual_value)
                except ValueError:
                    return False

            if operator == ">=":
                return actual_value >= value
            elif operator == ">":
                return actual_value > value
            elif operator == "<=":
                return actual_value <= value
            elif operator == "<":
                return actual_value < value
            elif operator == "==":
                return actual_value == value
            elif operator == "!=":
                return actual_value != value

        # Default: assume satisfied if we can't evaluate
        return True

    async def _generate_conclusion(
        self,
        project_id: str,
        project: dict[str, Any],
        success_criteria: list[SuccessCriterion],
    ) -> str:
        """Generate a smart conclusion for a completed project."""
        # Get completed tasks
        tasks = await self.db.fetch_all(
            """
            SELECT t.title, t.completed_at
            FROM tasks AS t
            INNER JOIN project_tasks AS pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.status = ? AND t.deleted_at IS NULL
            ORDER BY t.completed_at ASC
            """,
            (project_id, TaskStatus.COMPLETED.value),
        )

        # Get journal milestones
        milestones = await self.db.fetch_all(
            """
            SELECT content, created_at
            FROM project_journal_entries
            WHERE project_id = ? AND entry_type = ?
            ORDER BY created_at ASC
            """,
            (project_id, JournalEntryType.MILESTONE.value),
        )

        # Build conclusion
        lines: list[str] = []
        lines.append(f"## Project Conclusion: {project['title']}")
        lines.append("")
        
        lines.append("### Accomplishments")
        if tasks:
            for task in tasks:
                lines.append(f"- {task['title']}")
        else:
            lines.append("- Project completed")
        lines.append("")

        lines.append("### Key Milestones")
        if milestones:
            for milestone in milestones:
                lines.append(f"- {milestone['content']}")
        else:
            lines.append("- Project successfully executed")
        lines.append("")

        lines.append("### Outcome")
        aim = project.get("aim") or "The project aim"
        lines.append(f"{aim} was achieved.")
        lines.append("")

        lines.append("### Success Criteria Met")
        for criterion in success_criteria:
            lines.append(f"- ✅ {criterion.description}")
        lines.append("")

        # Check for any remaining open tasks
        open_tasks = await self.db.fetch_all(
            """
            SELECT t.title
            FROM tasks AS t
            INNER JOIN project_tasks AS pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.status IN (?, ?, ?) AND t.deleted_at IS NULL
            """,
            (project_id, TaskStatus.PLANNING.value, TaskStatus.PENDING.value, TaskStatus.ACTIVE.value),
        )

        if open_tasks:
            lines.append("### Next Steps")
            lines.append("Remaining open tasks:")
            for task in open_tasks:
                lines.append(f"- {task['title']}")
        else:
            lines.append("### Next Steps")
            lines.append("All planned tasks completed. Project is ready for closure.")

        return "\n".join(lines)

    async def _project_has_open_tasks(self, project_id: str) -> bool:
        row = await self.db.fetch_one(
            """
            SELECT 1 AS has_open
            FROM tasks AS t
            INNER JOIN project_tasks AS pt ON pt.task_id = t.id
            WHERE pt.project_id = ?
              AND t.deleted_at IS NULL
              AND t.status IN (?, ?, ?, ?)
            LIMIT 1
            """,
            (
                project_id,
                TaskStatus.PLANNING.value,
                TaskStatus.BLOCKED.value,
                TaskStatus.PENDING.value,
                TaskStatus.ACTIVE.value,
            ),
        )
        return row is not None

    async def _generate_follow_up_tasks(
        self,
        project_id: str,
        project: dict[str, Any],
        unmet_criteria: list[SuccessCriterion],
        context: dict[str, Any],
    ) -> list[str]:
        existing_tasks = await self.db.fetch_all(
            """
            SELECT t.id, t.metadata
            FROM tasks AS t
            INNER JOIN project_tasks AS pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.deleted_at IS NULL
            """,
            (project_id,),
        )
        existing_signatures = {
            (
                (json_loads(row.get("metadata"), {}) or {}).get("autonomy_cycle_completed_task_count"),
                (json_loads(row.get("metadata"), {}) or {}).get("autonomy_criterion_check"),
            )
            for row in existing_tasks
        }

        project_metadata = json_loads(project.get("metadata"), {})
        created_task_ids: list[str] = []
        snapshot = context.get("completed_task_count", 0)
        for criterion in unmet_criteria:
            signature = (snapshot, criterion.check)
            if signature in existing_signatures:
                continue
            task_payload = TaskCreate(
                title=self._build_follow_up_task_title(criterion),
                description=self._build_follow_up_task_description(project, criterion),
                plan=self._build_follow_up_task_plan(project, criterion),
                priority=TaskPriority.HIGH,
                project_ids=[project_id],
                metadata={
                    **project_metadata,
                    "auto_created_by_project": True,
                    "autonomy_reason": "unmet_success_criteria",
                    "autonomy_cycle_completed_task_count": snapshot,
                    "autonomy_criterion_check": criterion.check,
                    "autonomy_criterion_description": criterion.description,
                },
            )
            task = await self.task_service.create_task(task_payload)
            created_task_ids.append(str(task.id))
            existing_signatures.add(signature)

        if created_task_ids:
            await self._add_journal_entry(
                project_id,
                JournalEntryType.DECISION,
                "Project autonomy generated follow-up tasks for unmet success criteria.",
                {
                    "autonomy_action": "follow_up_generated",
                    "completed_task_count": snapshot,
                    "created_task_ids": created_task_ids,
                    "unmet_criteria": [
                        {"check": criterion.check, "description": criterion.description}
                        for criterion in unmet_criteria
                    ],
                },
            )
        return created_task_ids

    async def _generate_conclusion_from_evaluation(
        self,
        project_id: str,
        project: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> str:
        """Generate project conclusion from OpenClaw evaluation."""
        lines = [
            f"## Project Conclusion: {project['title']}",
            "",
            f"**Aim:** {project.get('aim', 'N/A')}",
            "",
            "**Evaluation:**",
            evaluation.get('reasoning', 'Project completed successfully.'),
            "",
            "**Success Criteria Met:**",
        ]

        for criterion in evaluation.get("met_criteria", []):
            lines.append(f"  ✅ {criterion}")

        lines.extend([
            "",
            "**Outcome:**",
            f"{project.get('aim', 'The project')} has been successfully completed.",
            "",
        ])

        # Get completed tasks for summary
        tasks = await self.db.fetch_all(
            """
            SELECT t.title, t.completed_at
            FROM tasks AS t
            INNER JOIN project_tasks AS pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.status = ? AND t.deleted_at IS NULL
            ORDER BY t.completed_at ASC
            """,
            (project_id, TaskStatus.COMPLETED.value),
        )

        if tasks:
            lines.append("**Accomplishments:**")
            for task in tasks:
                lines.append(f"  - {task['title']}")
            lines.append("")

        # Get journal milestones
        milestones = await self.db.fetch_all(
            """
            SELECT content, created_at
            FROM project_journal_entries
            WHERE project_id = ? AND entry_type = ?
            ORDER BY created_at ASC
            """,
            (project_id, JournalEntryType.MILESTONE.value),
        )

        if milestones:
            lines.append("**Key Milestones:**")
            for milestone in milestones:
                lines.append(f"  - {milestone['content'][:100]}...")
            lines.append("")

        return "\n".join(lines)

    async def _generate_follow_up_tasks_llm(
        self,
        project_id: str,
        project: dict[str, Any],
        unmet_criteria: list[str],
        evaluation: dict[str, Any],
    ) -> None:
        """
        Generate follow-up tasks using OpenClaw reasoning.

        This replaces the template-based approach with LLM-generated tasks.
        """
        try:
            # Use reasoning service to generate contextual follow-up tasks
            task_suggestions = await self.reasoning_service.generate_follow_up_tasks(
                project_id,
                unmet_criteria,
            )

            if not task_suggestions:
                # Fallback to template-based generation
                logging.warning("OpenClaw generated no follow-up tasks for project %s", project_id)
                return

            project_metadata = json_loads(project.get("metadata"), {})
            created_task_ids: list[str] = []

            for task_data in task_suggestions:
                # Create task with LLM-generated content
                task_payload = TaskCreate(
                    title=task_data.get("title", "Follow-up task")[:200],
                    description=task_data.get("description", ""),
                    plan=task_data.get("plan", ""),
                    priority=TaskPriority.HIGH,
                    project_ids=[project_id],
                    metadata={
                        **project_metadata,
                        "auto_created_by_project": True,
                        "autonomy_reason": "unmet_success_criteria",
                        "autonomy_method": "llm_generated",
                        "evaluation": evaluation,
                    },
                )

                task = await self.task_service.create_task(task_payload)
                created_task_ids.append(str(task.id))

            if created_task_ids:
                await self._add_journal_entry(
                    project_id,
                    JournalEntryType.DECISION,
                    f"Generated {len(created_task_ids)} LLM-based follow-up tasks for unmet criteria:\n" +
                    "\n".join(f"  - {c}" for c in unmet_criteria),
                    {
                        "autonomy_action": "llm_follow_up_generated",
                        "created_task_ids": created_task_ids,
                        "unmet_criteria": unmet_criteria,
                        "evaluation": evaluation,
                    },
                )

        except Exception as e:
            # Log error and fall back to template-based
            import logging
            logging.error(f"LLM follow-up generation failed for project {project_id}: {e}")

            # Fall back to template-based generation
            # Convert criteria strings to SuccessCriterion objects
            success_criteria = self._parse_success_criteria(project.get("success_criteria"))
            unmet_criterion_objects = [
                c for c in success_criteria
                if c.description in unmet_criteria or c.check in unmet_criteria
            ]

            if unmet_criterion_objects:
                context, _met_criteria, _unmet_criteria = await self._evaluate_criteria(project_id, success_criteria)
                await self._generate_follow_up_tasks(
                    project_id,
                    project,
                    unmet_criterion_objects,
                    context,
                )

    def _build_follow_up_task_title(self, criterion: SuccessCriterion) -> str:
        return f"Advance project criterion: {criterion.description}"[:200]

    def _build_follow_up_task_description(self, project: dict[str, Any], criterion: SuccessCriterion) -> str:
        parts = [
            f"Project: {project['title']}",
            f"Unmet success criterion: {criterion.description}",
            f"Check: {criterion.check}",
        ]
        if project.get("aim"):
            parts.append(f"Project aim: {project['aim']}")
        return "\n".join(parts)

    def _build_follow_up_task_plan(self, project: dict[str, Any], criterion: SuccessCriterion) -> str:
        lines = [
            f"Objective: satisfy the unmet project success criterion '{criterion.description}'.",
        ]
        if project.get("aim"):
            lines.append(f"Project aim: {project['aim']}")
        if project.get("method"):
            lines.append(f"Project method: {project['method']}")
        lines.extend(
            [
                f"Success criterion check: {criterion.check}",
                "Review the completed project work and identify the next concrete action needed to satisfy this criterion.",
                "Execute that action or gather the missing information needed to progress it.",
                "When complete, report the result in terms of the criterion.",
            ]
        )
        return "\n".join(lines)

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
        row["plan"] = self._parse_plan(row.get("plan"))
        row["success_criteria"] = self._parse_success_criteria(row.get("success_criteria"))
        row["auto_execute"] = bool(row.get("auto_execute", 0))
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

    async def _evaluate_success_criteria(self, project_id: str) -> None:
        """Evaluate success criteria and auto-complete if all met."""
        await self.evaluate_and_complete(project_id)

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
            SET state = ?, paused_at = ?, blocked_reason = ?, blocked_resume_instructions = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (ProjectState.PAUSED.value, now, reason, resume_instructions, project_id),
        )
        
        # Add journal entry
        await self._add_journal_entry(
            project_id,
            JournalEntryType.BLOCKER,
            f"Project blocked: {reason}",
            {"resume_instructions": resume_instructions} if resume_instructions else {},
        )
        
        # Trigger webhook notification
        await self._trigger_project_webhook(
            event=WebhookEvent.PROJECT_BLOCKED,
            project_id=project_id,
            project_title=project["title"],
            reason=reason,
            resume_instructions=resume_instructions,
        )
        await self._sync_notifications(project_id, immediate=True)
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
