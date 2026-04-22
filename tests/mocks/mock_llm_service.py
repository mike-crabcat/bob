"""Mock LLM service for deterministic testing without OpenClaw."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cyborg_server.database import Database
from cyborg_core.models import ProjectState, TaskStatus
from cyborg_server.services.base import utcnow


class MockLLMReasoningService:
    """Mock LLM service for deterministic testing without OpenClaw.

    This implements the same interface as OpenClawReasoningService but returns
    deterministic responses based on database state rather than calling an LLM.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._call_count = 0

    async def evaluate_success_criteria(self, project_id: str) -> dict[str, Any]:
        """Return evaluation based on project state (deterministic)."""
        self._call_count += 1

        # Get project and its criteria
        project = await self.db.fetch_one(
            """
            SELECT p.*, spec.success_criteria, spec.aim, spec.method
            FROM projects p
            LEFT JOIN project_specs spec ON spec.id = p.current_spec_id
            WHERE p.id = ? AND p.deleted_at IS NULL
            """,
            (project_id,),
        )

        if not project:
            return {
                "all_met": False,
                "met_criteria": [],
                "unmet_criteria": [],
                "reasoning": "Project not found",
            }

        # Get task statistics
        task_stats = await self.db.fetch_one(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as blocked
            FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.deleted_at IS NULL
            """,
            (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.BLOCKED.value, project_id),
        )

        # Parse success criteria
        import json
        from cyborg_server.services.base import json_loads

        criteria = json_loads(project.get("success_criteria") or "[]", [])
        completed_count = int(task_stats["completed"]) if task_stats and task_stats["completed"] else 0
        total_count = int(task_stats["total"]) if task_stats and task_stats["total"] else 0

        # Build context for evaluation
        context = {
            "completed_task_count": completed_count,
            "task_count": total_count,
            "failed_task_count": int(task_stats["failed"]) if task_stats and task_stats["failed"] else 0,
            "blocked_task_count": int(task_stats["blocked"]) if task_stats and task_stats["blocked"] else 0,
            "project_state": project["state"],
        }

        # Evaluate each criterion using simple pattern matching
        met_criteria = []
        unmet_criteria = []

        for criterion in criteria:
            check = criterion.get("check", "").lower()
            description = criterion.get("description", "")
            is_met = self._evaluate_check(check, context)

            if is_met:
                met_criteria.append(description)
            else:
                unmet_criteria.append(description)

        all_met = len(unmet_criteria) == 0

        return {
            "all_met": all_met,
            "met_criteria": met_criteria,
            "unmet_criteria": unmet_criteria,
            "reasoning": f"Mock evaluation: {completed_count}/{total_count} tasks complete. "
                        f"Met {len(met_criteria)} of {len(criteria)} criteria.",
        }

    def _evaluate_check(self, check: str, context: dict[str, Any]) -> bool:
        """Evaluate a single check expression against context."""
        import re

        # Simple pattern matching for common checks
        # e.g., "completed_task_count >= 2", "task_count > 5"
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

    async def refine_project_strategy(
        self,
        project_id: str,
        trigger_task_id: str,
    ) -> dict[str, Any]:
        """Return mock refinement response (no refinement needed for tests)."""
        self._call_count += 1

        # Get task that triggered refinement
        task = await self.db.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (trigger_task_id,),
        )

        # For testing, we'll generally say no refinement is needed
        # unless the task failed
        should_refine = task and task.get("status") == TaskStatus.FAILED.value

        if should_refine:
            return {
                "should_refine": True,
                "reasoning": f"Task '{task.get('title')}' failed, suggesting strategy adjustment (mock)",
                "suggested_changes": [
                    "Review task requirements",
                    "Adjust timeline estimates",
                ],
                "new_priorities": {},
                "risks_identified": [
                    "Previous approach may not work",
                ],
            }

        return {
            "should_refine": False,
            "reasoning": "Project progressing well (mock)",
            "suggested_changes": [],
            "new_priorities": {},
            "risks_identified": [],
        }

    async def extract_learnings(self, project_id: str) -> list[dict[str, Any]]:
        """Return mock learnings."""
        self._call_count += 1

        # Get project to determine outcome
        project = await self.db.fetch_one(
            "SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_id,),
        )

        if not project:
            return []

        outcome = "success" if project.get("state") == "closed" else "failure"

        return [
            {
                "category": "execution",
                "insight": f"Mock insight: Project outcome was {outcome}",
                "applicability": {
                    "keywords": ["test", "mock"],
                    "project_types": [project.get("aim", "")[:50]],
                },
            }
        ]

    async def generate_task_plan(self, task_id: str) -> str:
        """Return mock task plan."""
        self._call_count += 1

        task = await self.db.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (task_id,),
        )

        if not task:
            return "Mock plan: Task not found"

        return f"""Mock plan for: {task.get('title', 'Unknown Task')}

1. Review requirements
2. Execute main task
3. Verify results
4. Complete task

Generated by mock LLM service for testing.
"""

    async def analyze_project_health(self, project_id: str) -> dict[str, Any]:
        """Return mock health analysis."""
        self._call_count += 1

        # Get project and task stats
        project = await self.db.fetch_one(
            "SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_id,),
        )

        task_stats = await self.db.fetch_one(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as blocked
            FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.deleted_at IS NULL
            """,
            (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.BLOCKED.value, project_id),
        )

        total = int(task_stats["total"]) if task_stats and task_stats["total"] else 0
        failed = int(task_stats["failed"]) if task_stats and task_stats["failed"] else 0
        blocked = int(task_stats["blocked"]) if task_stats and task_stats["blocked"] else 0

        # Simple health score logic
        health_score = 1.0
        if total > 0:
            health_score = 1.0 - (failed + blocked) / total

        if health_score < 0.3:
            risk_level = "high"
        elif health_score < 0.7:
            risk_level = "medium"
        else:
            risk_level = "low"

        return {
            "health_score": health_score,
            "risk_level": risk_level,
            "indicators": {
                "total_tasks": total,
                "failed_tasks": failed,
                "blocked_tasks": blocked,
                "completion_rate": health_score,
            },
            "recommendations": [],
            "analysis": f"Mock health analysis for project {project_id}",
        }

    async def generate_follow_up_tasks(
        self,
        project_id: str,
        unmet_criteria: list[str],
    ) -> list[dict[str, Any]]:
        """Generate mock follow-up tasks based on unmet criteria."""
        self._call_count += 1

        if not unmet_criteria:
            return []

        # Generate one task per unmet criterion
        tasks = []
        for i, criterion in enumerate(unmet_criteria):
            # Extract target number from criterion if possible
            import re
            match = re.search(r'(\d+)\s*(tasks|items|goals)', criterion, re.IGNORECASE)

            if match:
                target = match.group(1)
                tasks.append({
                    "title": f"Complete additional work to meet criterion: {criterion}",
                    "description": f"Address unmet success criterion: {criterion}",
                    "plan": f"Work toward meeting: {criterion}\n1. Analyze current state\n2. Execute remaining work\n3. Verify completion",
                    "priority": "high",
                })
            else:
                tasks.append({
                    "title": f"Address criterion: {criterion}",
                    "description": f"Work needed to meet: {criterion}",
                    "plan": f"1. Review requirements\n2. Execute work\n3. Verify completion",
                    "priority": "high",
                })

        return tasks

    @property
    def call_count(self) -> int:
        """Return number of times methods were called (for test verification)."""
        return self._call_count

    def reset(self) -> None:
        """Reset call count."""
        self._call_count = 0
