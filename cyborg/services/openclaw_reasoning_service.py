"""LLM reasoning through OpenClaw gateway."""

from __future__ import annotations

import json
import logging
import ast
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from cyborg.database import Database
from cyborg.models import PlanStep, SuccessCriterion
from cyborg.services.base import BaseService, json_loads
from cyborg.services.context_builder import ContextBuilder, ContextScope
from cyborg.services.prompt_history import log_prompt

logger = logging.getLogger(__name__)

# Import structured logging helpers (lazy import to avoid circular dependency)
_structured_logger = None


def _get_structured_logger():
    """Lazy import structured logging helpers."""
    global _structured_logger
    if _structured_logger is None:
        from cyborg.structured_logging import get_logger as _get_logger
        _structured_logger = _get_logger(__name__)
    return _structured_logger


class OpenClawReasoningService(BaseService):
    """
    All LLM reasoning goes through OpenClaw.

    Cyborg builds context → OpenClaw does reasoning → Cyborg parses result
    """

    TIMEOUT_DEFAULT = 10800

    def __init__(self, db: Database):
        super().__init__(db)
        self.context_builder = ContextBuilder(db)

        # Lazy load OpenClawHookService to avoid circular import
        self._openclaw_service = None

    @property
    def openclaw_service(self):
        """Lazy-load OpenClawHookService."""
        if self._openclaw_service is None:
            from cyborg.services.openclaw_hook_service import OpenClawHookService
            from cyborg.services.session_route_service import SessionRouteService

            self._openclaw_service = OpenClawHookService(
                self.db,
                routing_service=SessionRouteService(self.db)
            )
        return self._openclaw_service

    async def generate_project_plan(
        self,
        aim: str,
        method: str | None = None,
        success_criteria: list[str] | None = None,
        reference_project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Ask OpenClaw to generate a project plan.
        """

        # Build minimal prompt
        prompt = self._build_plan_prompt(aim, method, success_criteria)

        # Call OpenClaw
        response = await self._call_openclaw(
            prompt=prompt,
            response_format="json",
            timeout=self.TIMEOUT_DEFAULT,
            reasoning_type="plan_generation",
            project_id=reference_project_id,
        )

        return self._parse_plan_response(response)

    async def evaluate_success_criteria(
        self,
        project_id: str,
    ) -> dict[str, Any]:
        """
        Ask OpenClaw to evaluate project success criteria.
        """

        # Build project context
        context = await self.context_builder.build_project_context(
            project_id=project_id,
            scope=ContextScope.STANDARD,  # Balance of detail and size
            focus_reasoning="evaluation",
        )

        # Build evaluation prompt
        prompt = self._build_evaluation_prompt(context)

        # Call OpenClaw
        response = await self._call_openclaw(
            prompt=prompt,
            response_format="json",
            timeout=self.TIMEOUT_DEFAULT,
            reasoning_type="criteria_evaluation",
            project_id=project_id,
        )

        return self._parse_evaluation_response(response)

    async def decide_next_step(
        self,
        project_id: str,
        completed_task_id: str,
    ) -> dict[str, Any]:
        """Ask OpenClaw what to do next after a task completes.

        Returns one of:
        - {"action": "create_task", "reasoning": "...", "task": {title, description, plan, priority}}
        - {"action": "close_project", "reasoning": "..."}
        - {"action": "block_project", "reasoning": "...", "block_reason": "...", "resume_instructions": "..."}
        """
        context = await self.context_builder.build_project_context(
            project_id=project_id,
            scope=ContextScope.STANDARD,
            focus_reasoning="next_action",
        )

        prompt = self._build_next_step_prompt(context, completed_task_id)

        try:
            response = await self._call_openclaw(
                prompt=prompt,
                response_format="json",
                timeout=self.TIMEOUT_DEFAULT,
                reasoning_type="next_action",
                project_id=project_id,
                task_id=completed_task_id,
            )
            return self._parse_next_step_response(response)
        except Exception as e:
            logger.error("decide_next_step failed for project %s: %s", project_id, e)
            return {
                "action": "block_project",
                "reasoning": f"Reasoning call failed: {e}",
                "block_reason": "Automated reasoning failed — manual review needed",
                "resume_instructions": "Review project state and manually create next task or close project",
            }

    async def refine_project_strategy(
        self,
        project_id: str,
        trigger_task_id: str,
    ) -> dict[str, Any]:
        """
        Ask OpenClaw to analyze and suggest strategy refinement.
        """

        # Build comprehensive context
        context = await self.context_builder.build_project_context(
            project_id=project_id,
            scope=ContextScope.COMPREHENSIVE,
            focus_reasoning="refinement",
        )

        # Build refinement prompt
        prompt = self._build_refinement_prompt(context, trigger_task_id)

        # Call OpenClaw
        response = await self._call_openclaw(
            prompt=prompt,
            response_format="json",
            timeout=self.TIMEOUT_DEFAULT,
            reasoning_type="strategy_refinement",
            project_id=project_id,
            task_id=trigger_task_id,
        )

        return self._parse_refinement_response(response)

    async def extract_learnings(
        self,
        project_id: str,
    ) -> list[dict[str, Any]]:
        """
        Ask OpenClaw to extract insights from a completed project.
        """

        context = await self.context_builder.build_project_context(
            project_id=project_id,
            scope=ContextScope.FULL,  # Need full context for learning
            focus_reasoning="learning",
        )

        prompt = self._build_learning_prompt(context)

        response = await self._call_openclaw(
            prompt=prompt,
            response_format="json",
            timeout=self.TIMEOUT_DEFAULT,
            reasoning_type="learning_extraction",
            project_id=project_id,
        )

        return self._parse_learning_response(response)

    async def generate_task_plan(
        self,
        task_id: str,
    ) -> str:
        """
        Ask OpenClaw to generate a plan for a specific task.
        """

        task = await self.db.fetch_one(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,)
        )

        if not task:
            raise ValueError(f"Task {task_id} not found")

        # Get project context if task is linked to a project
        project_context = {}
        project_links = await self.db.fetch_all(
            "SELECT project_id FROM project_tasks WHERE task_id = ?",
            (task_id,)
        )
        project_id_from_task = None
        if project_links:
            project_id_from_task = project_links[0]["project_id"]
            project_context = await self.context_builder.build_project_context(
                project_id=project_id_from_task,
                scope=ContextScope.MINIMAL,
                focus_reasoning="task_planning",
            )

        # Resolve output directory and dependency files from task metadata
        task_metadata = json_loads(task.get("metadata"), {})
        output_directory: str | None = task_metadata.get("output_directory")
        dependency_files: list[dict[str, Any]] | None = task_metadata.get("dependency_output_files")

        # If not in metadata, compute from project link
        if output_directory is None and project_id_from_task:
            try:
                from cyborg.services.task_service import TaskService

                task_service = TaskService(self.db)
                output_directory = await task_service._compute_output_directory(task_id)
            except Exception:
                pass

        prompt = self._build_task_plan_prompt(
            dict(task),
            project_context,
            output_directory=output_directory,
            dependency_files=dependency_files,
        )

        response = await self._call_openclaw(
            prompt=prompt,
            response_format="text",
            timeout=self.TIMEOUT_DEFAULT,
            reasoning_type="task_planning",
            task_id=task_id,
            project_id=project_id_from_task,
        )

        return response.strip()

    async def analyze_project_health(
        self,
        project_id: str,
    ) -> dict[str, Any]:
        """
        Ask OpenClaw to analyze project health and risks.
        """

        context = await self.context_builder.build_project_context(
            project_id=project_id,
            scope=ContextScope.STANDARD,
            focus_reasoning="health",
        )

        prompt = self._build_health_analysis_prompt(context)

        response = await self._call_openclaw(
            prompt=prompt,
            response_format="json",
            timeout=self.TIMEOUT_DEFAULT,
            reasoning_type="health_analysis",
            project_id=project_id,
        )

        return self._parse_health_analysis_response(response)

    async def generate_follow_up_tasks(
        self,
        project_id: str,
        unmet_criteria: list[str],
    ) -> list[dict[str, Any]]:
        """
        Ask OpenClaw to suggest follow-up tasks for unmet project criteria.
        """

        context = await self.context_builder.build_project_context(
            project_id=project_id,
            scope=ContextScope.STANDARD,
            focus_reasoning="follow_up_generation",
        )

        prompt = self._build_follow_up_tasks_prompt(context, unmet_criteria)

        response = await self._call_openclaw(
            prompt=prompt,
            response_format="json",
            timeout=self.TIMEOUT_DEFAULT,
            reasoning_type="follow_up_generation",
            project_id=project_id,
        )

        return self._parse_follow_up_tasks_response(response)

    async def revise_spec(
        self,
        aim: str,
        method: str | None,
        success_criteria: list[dict[str, str]],
        plan_steps: list[dict[str, Any]],
        feedback: str,
        *,
        allow_aim_changes: bool = False,
        allow_criteria_changes: bool = False,
        reference_project_id: str | None = None,
        current_tasks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Ask OpenClaw to revise a spec based on feedback.

        Returns a dict with revised aim, method, success_criteria, plan,
        and optionally deprecated_task_ids for tasks made obsolete by the revision.
        Returns None if revision fails.
        """
        constraints = []
        if not allow_aim_changes:
            constraints.append("The aim MUST stay exactly as-is — do not change it.")
        else:
            constraints.append("You MAY revise the aim based on the feedback.")

        if not allow_criteria_changes:
            constraints.append("The success criteria MUST stay exactly as-is — do not change them.")
        else:
            constraints.append("You MAY revise the success criteria based on the feedback.")

        criteria_lines = "\n".join(
            f"  - {c.get('check', c.get('description', ''))}" for c in success_criteria
        )
        plan_lines = "\n".join(
            f"  {i}. {s.get('title', '?')}: {s.get('description', '')}" for i, s in enumerate(plan_steps)
        )

        parts = [
            "You are a project planning assistant. Revise the spec based on the feedback below.",
            "",
            f"Current Aim: {aim}",
            f"Current Method: {method or 'Not specified'}",
            "",
            "Current Success Criteria:",
            criteria_lines or "  (none)",
            "",
            "Current Plan:",
            plan_lines or "  (none)",
        ]

        # Include current project tasks so reasoning can identify obsolete ones
        if current_tasks:
            parts.extend(["", "Current Project Tasks:"])
            for t in current_tasks:
                status = t.get("status", "?")
                title = t.get("title", "Untitled")
                result = f": {t['result'][:100]}" if t.get("result") else ""
                parts.append(f'  - [{t["id"]}] [{status}] {title}{result}')

        parts.extend([
            "",
            f"Feedback: {feedback}",
            "",
            "Constraints:",
        ] + [f"  - {c}" for c in constraints] + [
            "",
            "Important: If the revised spec makes any existing tasks obsolete (e.g. the aim changed",
            "direction, criteria were replaced, or the feedback requests a different approach), list",
            "their IDs in deprecated_task_ids. Deprecated tasks will be excluded from future reasoning.",
            "Do NOT deprecate tasks whose output is still relevant or could be built upon.",
            "",
            "Respond with valid JSON only:",
            "{",
            '  "aim": "revised aim (or original if unchanged)",',
            '  "method": "revised method",',
            '  "success_criteria": [{"check": "...", "description": "..."}],',
            '  "plan": [{"order": 0, "title": "...", "description": "...", "criteria": "..."}],',
            '  "deprecated_task_ids": ["task-id-1", "task-id-2"]',
            "}",
        ])

        prompt = "\n".join(parts)

        try:
            response = await self._call_openclaw(
                prompt=prompt,
                response_format="json",
                timeout=self.TIMEOUT_DEFAULT,
                reasoning_type="spec_revision",
                project_id=reference_project_id,
            )
            return self._load_json_payload(response)
        except Exception as e:
            logger.warning("Spec revision failed for project %s: %s", reference_project_id, e)
            return None

    async def _call_openclaw(
        self,
        prompt: str,
        response_format: str = "text",
        timeout: int = 30,
        session_key: str | None = None,
        reasoning_type: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> str:
        """
        Call OpenClaw gateway for reasoning.

        Uses a separate internal session for reasoning (not user-facing).
        """
        from cyborg.structured_logging import log_reasoning_request

        # Use a fresh session for each reasoning call
        if session_key:
            reasoning_session = session_key
        else:
            short_uuid = str(uuid4())[:8]
            type_slug = (reasoning_type or "unknown").replace("_", "-")
            reasoning_session = f"cyborg:reasoning:{type_slug}:{short_uuid}"

        if not self.openclaw_service.is_configured():
            log_reasoning_request(
                _get_structured_logger(),
                reasoning_type or "unknown",
                project_id=project_id,
                task_id=task_id,
                success=False,
                error="OpenClaw not configured",
            )
            raise RuntimeError("OpenClaw reasoning is not configured")

        # Build gateway params
        params = {
            "message": prompt,
            "deliver": False,  # Not delivering to a user
            "sessionKey": reasoning_session,
            "thinking": "high",
            "timeout": timeout * 1000,
            "idempotencyKey": str(uuid4()),
        }

        # Add response format hint
        if response_format == "json":
            params["message"] += "\n\nIMPORTANT: Respond with valid JSON only. No markdown formatting, no code blocks, no explanation outside the JSON."

        # Log the prompt to prompt_history
        await log_prompt(
            self.db,
            category=reasoning_type or "unknown",
            prompt_text=params["message"],
            project_id=project_id,
            task_id=task_id,
            session_key=reasoning_session,
        )

        # Track timing
        start_time = datetime.now(timezone.utc)

        # Log request start
        log_reasoning_request(
            _get_structured_logger(),
            reasoning_type or "unknown",
            project_id=project_id,
            task_id=task_id,
            timeout_seconds=timeout,
            response_format=response_format,
        )

        # Call gateway
        try:
            response = await self.openclaw_service._send_gateway_request(
                method="agent",
                params=params,
                expect_final=True,
                timeout_seconds=timeout,
            )

            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            response_text = self._extract_response_text(response)

            # Log success
            log_reasoning_request(
                _get_structured_logger(),
                reasoning_type or "unknown",
                project_id=project_id,
                task_id=task_id,
                duration_seconds=duration,
                success=True,
                response_length=len(response_text),
            )

            return response_text

        except Exception as e:
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()

            # Log failure
            log_reasoning_request(
                _get_structured_logger(),
                reasoning_type or "unknown",
                project_id=project_id,
                task_id=task_id,
                duration_seconds=duration,
                success=False,
                error=str(e),
                error_type=type(e).__name__,
            )

            logger.error("OpenClaw reasoning call failed: %s", e)
            raise RuntimeError(f"OpenClaw reasoning failed: {e}") from e

    def _format_upstream_context_for_prompt(
        self,
        context: dict[str, Any],
        task_id: str | None = None,
    ) -> str:
        """Format upstream task context into prompt-ready text.

        If task_id is provided, only show upstream for that specific task.
        Otherwise, show all upstream context.
        """
        tasks_context = context.get("tasks", {})
        upstream = tasks_context.get("upstream_context", {})
        if not upstream:
            return ""

        parts = ["", "## Upstream Task Results (Prior Work)"]

        targets = {task_id: upstream[task_id]} if (task_id and task_id in upstream) else upstream

        for _dep_task_id, parent_info in targets.items():
            parts.append(f"### Parent Task: {parent_info.get('title', 'Unknown')}")
            parts.append(f"Status: {parent_info.get('status', 'unknown')}")
            if parent_info.get("result"):
                result_text = parent_info["result"][:500]
                parts.append(f"Result: {result_text}")
            if parent_info.get("completed_at"):
                parts.append(f"Completed: {parent_info['completed_at']}")

            output_files = parent_info.get("output_files", [])
            if output_files:
                parts.append("Output Files:")
                for f in output_files:
                    path = f.get("relative_path", f.get("filename", "unknown"))
                    purpose = f.get("purpose", "")
                    parts.append(f"  - {path} ({purpose})")

            dep_files = parent_info.get("dependency_output_files", [])
            if dep_files:
                parts.append("Available Input Files:")
                for f in dep_files:
                    path = f.get("relative_path", f.get("filename", "unknown"))
                    purpose = f.get("purpose", "")
                    parts.append(f"  - {path} ({purpose})")

            parts.append("")

        return "\n".join(parts)

    def _build_plan_prompt(
        self,
        aim: str,
        method: str | None,
        success_criteria: list[str] | None,
    ) -> str:
        """Build prompt for plan generation."""

        parts = [
            "You are a project planning assistant. Generate a structured execution plan.",
            "",
            f"Project Aim: {aim}",
        ]

        if method:
            parts.append(f"Method: {method}")

        if success_criteria:
            parts.append("")
            parts.append("Success Criteria:")
            for i, criterion in enumerate(success_criteria, 1):
                parts.append(f"  {i}. {criterion}")

        parts.extend([
            "",
            "Generate a plan with 3-8 steps. Each step should have:",
            "  - title: Brief step name",
            "  - description: What needs to be done",
            "  - criteria: How to know this step is complete",
            "",
            "Respond with valid JSON only:",
            "{",
            '  "steps": [',
            '    {"order": 0, "title": "...", "description": "...", "criteria": "..."},',
            '    ...',
            '  ]',
            "}",
        ])

        return "\n".join(parts)

    def _build_evaluation_prompt(self, context: dict[str, Any]) -> str:
        """Build prompt for success criteria evaluation."""

        core = context["core"]
        criteria = core["success_criteria"]["criteria"]
        task_summary = context["tasks"]["summary"]
        journal = context["journal"].get("entries", [])[-10:]

        parts = [
            "You are evaluating whether a project has achieved its success criteria.",
            "",
            f"Project: {core['project']['title']}",
            f"Aim: {core['project'].get('aim', 'N/A')}",
            "",
            "Success Criteria to Evaluate:",
        ]

        for i, criterion in enumerate(criteria, 1):
            desc = criterion.get("description", "")
            check = criterion.get("check", "")
            parts.append(f"  {i}. {desc}")
            if check:
                parts.append(f"     Check: {check}")

        parts.extend([
            "",
            "Current State:",
            f"  - Total tasks: {task_summary['total']}",
            f"  - Completed: {task_summary['completed']}",
            f"  - Failed: {task_summary['failed']}",
            f"  - Active: {task_summary['active']}",
            "",
            "Recent Journal:",
        ])

        for entry in journal[-5:]:
            content = entry.get("content", "")[:100]
            parts.append(f"  - [{entry['entry_type']}] {content}...")

        parts.extend([
            "",
        ])

        upstream_text = self._format_upstream_context_for_prompt(context)
        if upstream_text:
            parts.append(upstream_text)

        parts.extend([
            "",
            "Evaluate each criterion based on available evidence.",
            "Respond with valid JSON only:",
            "{",
            '  "all_met": true or false,',
            '  "met_criteria": ["criterion 1", "criterion 2"],',
            '  "unmet_criteria": ["criterion 3"],',
            '  "reasoning": "Brief explanation of the evaluation..."',
            "}",
        ])

        return "\n".join(parts)

    def _build_refinement_prompt(
        self,
        context: dict[str, Any],
        trigger_task_id: str,
    ) -> str:
        """Build prompt for strategy refinement."""

        core = context["core"]
        tasks = context["tasks"]
        journal = context["journal"].get("entries", [])

        # Find trigger task
        trigger_task = None
        for task in tasks.get("tasks", []):
            if task.get("id") == trigger_task_id:
                trigger_task = task
                break

        trigger_info = ""
        if trigger_task:
            status = trigger_task.get("status", "unknown")
            result = (trigger_task.get("result") or "")[:200]
            trigger_info = f"Status: {status}, Result: {result}"

        parts = [
            "Analyze this project's progress and suggest strategic refinements.",
            "",
            f"Project: {core['project']['title']}",
            f"Aim: {core['project'].get('aim', 'N/A')}",
            f"Current State: {core['project']['state']}",
            "",
            f"Trigger: Task {trigger_task_id} just completed.",
            trigger_info,
            "",
            "Task Summary:",
            f"  - Total: {tasks['summary']['total']}",
            f"  - Completed: {tasks['summary']['completed']}",
            f"  - Failed: {tasks['summary']['failed']}",
            f"  - Active: {tasks['summary']['active']}",
            "",
            "Recent Activity:",
        ]

        for entry in journal[-5:]:
            content = entry.get("content", "")[:150]
            parts.append(f"  - [{entry['entry_type']}] {content}")

        upstream_text = self._format_upstream_context_for_prompt(context)
        if upstream_text:
            parts.append(upstream_text)

        parts.extend([
            "",
            "Consider:",
            "1. Is the current plan still optimal?",
            "2. Are there blockers or risks?",
            "3. Should tasks be re-prioritized?",
            "4. Are additional steps needed?",
            "",
            "Respond with valid JSON only:",
            "{",
            '  "should_refine": true or false,',
            '  "reasoning": "...",',
            '  "suggested_changes": [',
            '    {"type": "add_task|remove_task|reprioritize|change_approach", "description": "..."}',
            '  ],',
            '  "new_priorities": {"task_id": "high|medium|low"},',
            '  "risks_identified": ["..."]',
            "}",
        ])

        return "\n".join(parts)

    def _build_next_step_prompt(self, context: dict[str, Any], completed_task_id: str) -> str:
        """Build prompt for deciding the next action after a task completes."""

        core = context["core"]
        project = core["project"]
        criteria = core["success_criteria"].get("criteria", [])
        plan_steps = core["success_criteria"].get("plan_steps", [])
        tasks = context["tasks"]
        task_summary = tasks["summary"]
        journal = context["journal"].get("entries", [])[-10:]

        # Find the completed task in the context
        completed_task_info = ""
        all_tasks = tasks.get("all_tasks", [])
        for t in all_tasks:
            if t.get("id") == completed_task_id:
                completed_task_info = f"Title: {t.get('title', 'Unknown')}\nStatus: {t.get('status', 'unknown')}"
                if t.get("result"):
                    completed_task_info += f"\nResult: {t['result'][:500]}"
                break

        # If not found in all_tasks, check upstream
        if not completed_task_info:
            upstream = tasks.get("upstream_context", {})
            if completed_task_id in upstream:
                info = upstream[completed_task_id]
                completed_task_info = f"Title: {info.get('title', 'Unknown')}\nStatus: {info.get('status', 'unknown')}"
                if info.get("result"):
                    completed_task_info += f"\nResult: {info['result'][:500]}"

        if not completed_task_info:
            completed_task_info = f"Task ID: {completed_task_id} (details not available)"

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

        # Plan as reference
        if plan_steps:
            parts.extend(["", "## Plan (Reference — use as guidance, not rigid script)"])
            for i, step in enumerate(plan_steps):
                title = step.get("title", f"Step {i + 1}")
                parts.append(f"  {i + 1}. {title}")

        # Success criteria
        if criteria:
            parts.extend(["", "## Success Criteria"])
            for i, c in enumerate(criteria, 1):
                parts.append(f"  {i}. {c.get('description', '')}")

        # Just completed task
        parts.extend(["", "## Just Completed", completed_task_info])

        # Completed tasks summary
        completed_list = [t for t in all_tasks if t.get("status") == "completed"]
        if completed_list:
            parts.extend(["", "## All Completed Tasks"])
            for t in completed_list:
                result_bit = f": {t['result'][:100]}" if t.get("result") else ""
                parts.append(f"  - {t.get('title', 'Unknown')}{result_bit}")

        # Current open tasks
        open_tasks = [t for t in all_tasks if t.get("status") in ("pending", "active", "blocked")]
        if open_tasks:
            parts.extend(["", "## Current Open Tasks"])
            for t in open_tasks:
                parts.append(f"  - [{t.get('status', '?')}] {t.get('title', 'Unknown')}")

        # Recent journal
        if journal:
            parts.extend(["", "## Recent Activity"])
            for entry in journal[-5:]:
                content = entry.get("content", "")[:120]
                parts.append(f"  - [{entry.get('entry_type', '?')}] {content}")

        # Upstream context
        upstream_text = self._format_upstream_context_for_prompt(context)
        if upstream_text:
            parts.append(upstream_text)

        parts.extend([
            "",
            "Based on the above, decide the single best next action:",
            "",
            "1. **create_task** — The project needs another task to progress toward its aim.",
            "   Provide a focused task with title, description, and plan.",
            "2. **close_project** — All success criteria appear to be met. The project is done.",
            "3. **block_project** — The project needs human input before it can continue.",
            "",
            "Respond with valid JSON only:",
            "{",
            '  "action": "create_task | close_project | block_project",',
            '  "reasoning": "Brief explanation of why this action",',
            '  "task": {',
            '    "title": "...",',
            '    "description": "...",',
            '    "plan": "Objective: ...\\nExecution: ...\\nSuccess criteria: ...",',
            '    "priority": "high | medium | low"',
            "  },",
            '  "block_reason": "Why the project is blocked",',
            '  "resume_instructions": "What needs to happen to unblock"',
            "}",
        ])

        return "\n".join(parts)

    def _build_learning_prompt(self, context: dict[str, Any]) -> str:
        """Build prompt for learning extraction."""

        core = context["core"]
        journal = context["journal"].get("entries", [])
        tasks = context["tasks"]["summary"]

        parts = [
            "Extract insights and learnings from this completed project.",
            "",
            f"Project: {core['project']['title']}",
            f"Aim: {core['project'].get('aim', 'N/A')}",
            f"Duration: {core['project'].get('duration_days', '?')} days",
            f"Outcome: {core['project'].get('state', 'unknown')}",
            "",
            "Tasks:",
            f"  - Total: {tasks['total']}",
            f"  - Completed: {tasks['completed']}",
            f"  - Failed: {tasks['failed']}",
            "",
        ]

        upstream_text = self._format_upstream_context_for_prompt(context)
        if upstream_text:
            parts.append(upstream_text)

        parts.extend([
            "Full Journal:",
        ])

        for entry in journal[-20:]:  # Last 20 entries
            parts.append(f"  - [{entry['entry_type']}] {entry['content'][:150]}")

        parts.extend([
            "",
            "Extract:",
            "1. What worked well?",
            "2. What didn't work?",
            "3. What would you do differently?",
            "4. Patterns that could apply to future projects?",
            "",
            "Respond with valid JSON only:",
            "{",
            '  "insights": [',
            '    {',
            '      "category": "planning|execution|estimation|communication|technical",',
            '      "lesson": "What was learned",',
            '      "applicability": "when to apply this",',
            '      "impact": "positive|negative|neutral"',
            '    }',
            '  ],',
            '  "success_patterns": ["..."],',
            '  "failure_patterns": ["..."],',
            '  "recommendations": ["..."]',
            "}",
        ])

        return "\n".join(parts)

    def _build_task_plan_prompt(
        self,
        task: dict[str, Any],
        project_context: dict[str, Any],
        output_directory: str | None = None,
        dependency_files: list[dict[str, Any]] | None = None,
    ) -> str:
        """Build prompt for task-level planning."""

        parts = [
            "Generate an execution plan for this task.",
            "",
            f"Task: {task['title']}",
        ]

        if task.get('description'):
            parts.append(f"Description: {task['description']}")

        if project_context:
            project = project_context.get("core", {}).get("project", {})
            if project.get("title"):
                parts.extend([
                    "",
                    f"Parent Project: {project['title']}",
                ])
            if project.get("aim"):
                parts.append(f"Project Aim: {project['aim']}")

        if output_directory:
            parts.extend([
                "",
                "## Output Directory",
                f"All task artifacts must be written to: `{output_directory}`",
                "- Use descriptive filenames for each artifact.",
                "- Put the primary result in `RESULT.md`.",
                "- Register all output files via the task files API.",
            ])

        if dependency_files:
            parts.extend([
                "",
                "## Input Files from Previous Task",
                "The following files were produced by a dependency task and are available as inputs:",
                "",
            ])
            for f in dependency_files:
                parts.append(f"- `{f.get('relative_path', f.get('filename', 'unknown'))}` ({f.get('purpose', 'unknown')})")

        # Include upstream task results from project context
        upstream_text = self._format_upstream_context_for_prompt(
            project_context,
            task_id=task.get("id"),
        )
        if upstream_text:
            parts.append(upstream_text)

        parts.extend([
            "",
            "Generate a concise plan (3-5 bullet points) for executing this task.",
            "Be specific and actionable.",
        ])

        return "\n".join(parts)

    def _build_health_analysis_prompt(self, context: dict[str, Any]) -> str:
        """Build prompt for project health analysis."""

        core = context["core"]
        tasks = context["tasks"]
        journal = context["journal"].get("entries", [])

        # Find blocked tasks
        blocked_tasks = [t for t in tasks.get("tasks", []) if t.get("status") == "blocked"]

        parts = [
            "Analyze the health of this project and identify risks.",
            "",
            f"Project: {core['project']['title']}",
            f"State: {core['project']['state']}",
            f"Duration: {core['project'].get('duration_days', '?')} days",
            "",
            "Task Summary:",
            f"  - Total: {tasks['summary']['total']}",
            f"  - Completed: {tasks['summary']['completed']}",
            f"  - Active: {tasks['summary']['active']}",
            f"  - Blocked: {tasks['summary']['blocked']}",
            f"  - Failed: {tasks['summary']['failed']}",
            "",
        ]

        if blocked_tasks:
            parts.append("Blocked Tasks:")
            for task in blocked_tasks[:5]:
                reason = task.get("blocked_reason", "No reason")[:100]
                parts.append(f"  - {task['title']}: {reason}")

        upstream_text = self._format_upstream_context_for_prompt(context)
        if upstream_text:
            parts.append(upstream_text)

        parts.extend([
            "",
            "Recent Journal (concerns, decisions, blockers):",
        ])

        concerning_entries = [e for e in journal if e["entry_type"] in ["blocker", "decision"]]
        for entry in concerning_entries[-5:]:
            parts.append(f"  - [{entry['entry_type']}] {entry['content'][:150]}")

        parts.extend([
            "",
            "Assess:",
            "1. Is this project at risk?",
            "2. What are the critical blockers?",
            "3. Is the schedule at risk?",
            "4. What immediate actions are needed?",
            "",
            "Respond with valid JSON only:",
            "{",
            '  "health_status": "healthy|at_risk|critical",',
            '  "risk_level": "low|medium|high|critical",',
            '  "blockers": [',
            '    {"task": "...", "severity": "low|medium|high", "recommendation": "..."}',
            '  ],',
            '  "schedule_risk": "on_track|at_risk|behind",',
            '  "recommendations": [',
            '    {"priority": "high|medium|low", "action": "..."}',
            '  ],',
            '  "escalation_required": true or false,',
            '  "reasoning": "..."',
            "}",
        ])

        return "\n".join(parts)

    def _build_follow_up_tasks_prompt(
        self,
        context: dict[str, Any],
        unmet_criteria: list[str],
    ) -> str:
        """Build prompt for generating follow-up tasks."""

        core = context["core"]
        tasks = context["tasks"]
        journal = context["journal"].get("entries", [])

        parts = [
            "Generate follow-up tasks for this project.",
            "",
            f"Project: {core['project']['title']}",
            f"Aim: {core['project'].get('aim', 'N/A')}",
            f"Method: {core['project'].get('method', 'N/A')}",
            "",
            "Task Summary:",
            f"  - Total: {tasks['summary']['total']}",
            f"  - Completed: {tasks['summary']['completed']}",
            f"  - Failed: {tasks['summary']['failed']}",
            f"  - Blocked: {tasks['summary']['blocked']}",
            "",
            "Unmet Criteria:",
        ]

        for criterion in unmet_criteria:
            parts.append(f"  - {criterion}")

        if journal:
            parts.extend(
                [
                    "",
                    "Recent Journal:",
                ]
            )
            for entry in journal[-5:]:
                parts.append(f"  - [{entry['entry_type']}] {entry['content'][:150]}")

        upstream_text = self._format_upstream_context_for_prompt(context)
        if upstream_text:
            parts.append(upstream_text)

        parts.extend(
            [
                "",
                "Suggest 1-3 concrete follow-up tasks that would help satisfy the unmet criteria.",
                "Each task must include:",
                "  - title",
                "  - description",
                "  - plan",
                "  - priority (low|medium|high|critical)",
                "",
                "Respond with valid JSON only:",
                "{",
                '  "tasks": [',
                '    {"title": "...", "description": "...", "plan": "...", "priority": "high"}',
                "  ]",
                "}",
            ]
        )

        return "\n".join(parts)

    def _parse_plan_response(self, response: str) -> list[dict[str, Any]]:
        """Parse JSON plan response."""
        try:
            data = self._load_json_payload(response)
            if isinstance(data, dict) and "steps" in data:
                return data["steps"]
            if isinstance(data, list):
                return data
            return []
        except json.JSONDecodeError as e:
            logger.error("Failed to parse plan response: %s", e)
            return []

    def _parse_evaluation_response(self, response: str) -> dict[str, Any]:
        """Parse JSON evaluation response."""
        try:
            return self._load_json_payload(response)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse evaluation response: %s", e)
            return {
                "all_met": False,
                "met_criteria": [],
                "unmet_criteria": [],
                "reasoning": f"Failed to parse evaluation: {e}"
            }

    def _parse_next_step_response(self, response: str) -> dict[str, Any]:
        """Parse JSON next-step decision response."""
        try:
            data = self._load_json_payload(response)
            action = data.get("action", "")
            if action not in ("create_task", "close_project", "block_project"):
                logger.warning("Unknown next_step action '%s', defaulting to block_project", action)
                return {
                    "action": "block_project",
                    "reasoning": data.get("reasoning", f"Unknown action '{action}' returned"),
                    "block_reason": f"Reasoning returned unknown action: {action}",
                    "resume_instructions": "Review project state manually",
                }
            result = {
                "action": action,
                "reasoning": data.get("reasoning", ""),
            }
            if action == "create_task":
                task_def = data.get("task", {})
                result["task"] = {
                    "title": str(task_def.get("title", "Next step"))[:200],
                    "description": str(task_def.get("description", ""))[:2000],
                    "plan": str(task_def.get("plan", "")),
                    "priority": str(task_def.get("priority", "high")),
                }
            elif action == "block_project":
                result["block_reason"] = str(data.get("block_reason", "Project blocked by reasoning"))[:500]
                result["resume_instructions"] = str(data.get("resume_instructions", ""))[:2000]
            return result
        except (json.JSONDecodeError, Exception) as e:
            logger.error("Failed to parse next_step response: %s", e)
            return {
                "action": "block_project",
                "reasoning": f"Failed to parse response: {e}",
                "block_reason": "Reasoning response could not be parsed",
                "resume_instructions": "Review project state and manually create next task or close project",
            }

    def _parse_refinement_response(self, response: str) -> dict[str, Any]:
        """Parse JSON refinement response."""
        try:
            return self._load_json_payload(response)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse refinement response: %s", e)
            return {
                "should_refine": False,
                "reasoning": f"Failed to parse refinement: {e}",
                "suggested_changes": [],
                "new_priorities": {},
                "risks_identified": []
            }

    def _parse_learning_response(self, response: str) -> list[dict[str, Any]]:
        """Parse JSON learning response."""
        try:
            data = self._load_json_payload(response)
            if isinstance(data, dict) and "insights" in data:
                return data["insights"]
            if isinstance(data, list):
                return data
            return []
        except json.JSONDecodeError as e:
            logger.error("Failed to parse learning response: %s", e)
            return []

    def _parse_health_analysis_response(self, response: str) -> dict[str, Any]:
        """Parse JSON health analysis response."""
        try:
            return self._load_json_payload(response)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse health analysis response: %s", e)
            return {
                "health_status": "unknown",
                "risk_level": "unknown",
                "blockers": [],
                "schedule_risk": "unknown",
                "recommendations": [],
                "escalation_required": False,
                "reasoning": f"Failed to parse health analysis: {e}"
            }

    def _parse_follow_up_tasks_response(self, response: str) -> list[dict[str, Any]]:
        """Parse JSON follow-up task suggestions."""

        try:
            data = self._load_json_payload(response)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse follow-up task response: %s", e)
            return []

        raw_tasks: list[Any]
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            raw_tasks = data["tasks"]
        elif isinstance(data, list):
            raw_tasks = data
        else:
            return []

        tasks: list[dict[str, Any]] = []
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            description = str(item.get("description", "")).strip()
            plan = str(item.get("plan", "")).strip()
            if not plan:
                plan = self._default_follow_up_task_plan(title=title, description=description)
            priority = str(item.get("priority", "high")).strip().lower() or "high"
            if priority not in {"low", "medium", "high", "critical"}:
                priority = "high"
            tasks.append(
                {
                    "title": title,
                    "description": description,
                    "plan": plan,
                    "priority": priority,
                }
            )
        return tasks

    def _default_follow_up_task_plan(self, *, title: str, description: str) -> str:
        """Fallback plan when the model omits one."""
        lines = [f"Objective: {title}"]
        if description:
            lines.append(f"Context: {description}")
        lines.extend(
            [
                "Identify the next concrete action required.",
                "Complete that action or gather the missing information.",
                "Report the result in terms of the unmet project criterion.",
            ]
        )
        return "\n".join(lines)

    def _extract_response_text(self, response: Any) -> str:
        """Extract the most useful text payload from an OpenClaw gateway response."""

        if isinstance(response, str):
            return response.strip()

        if isinstance(response, dict):
            result = response.get("result")
            if isinstance(result, dict):
                payload_text = self._extract_payload_text(result.get("payloads"))
                if payload_text:
                    return payload_text
                for key in ("content", "text", "message"):
                    value = result.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()

            payload_text = self._extract_payload_text(response.get("payloads"))
            if payload_text:
                return payload_text

            for key in ("content", "text", "message"):
                value = response.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

            for key in ("summary",):
                value = response.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        return str(response)

    def _extract_payload_text(self, payloads: Any) -> str:
        """Extract text from OpenClaw payload arrays."""

        if not isinstance(payloads, list):
            return ""
        parts: list[str] = []
        for item in payloads:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()

    def _load_json_payload(self, response: str) -> Any:
        """Load JSON from a strict or lightly wrapped model response."""

        candidates: list[str] = []
        stripped = response.strip()
        if not stripped:
            raise json.JSONDecodeError("empty response", response, 0)

        candidates.append(stripped)
        fenced = re.findall(r"```(?:json)?\s*(.*?)```", stripped, flags=re.DOTALL | re.IGNORECASE)
        candidates.extend(block.strip() for block in fenced if block.strip())

        first_object = stripped.find("{")
        first_array = stripped.find("[")
        starts = [index for index in (first_object, first_array) if index != -1]
        if starts:
            start = min(starts)
            candidates.append(stripped[start:].strip())

        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
            try:
                value = ast.literal_eval(candidate)
            except (SyntaxError, ValueError):
                continue
            if isinstance(value, (dict, list)):
                return value

        raise json.JSONDecodeError("unable to parse JSON payload", response, 0)
