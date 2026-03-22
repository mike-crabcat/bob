"""LLM reasoning through OpenClaw gateway."""

from __future__ import annotations

import json
import logging
import ast
import re
from typing import Any
from uuid import uuid4

from cyborg.database import Database
from cyborg.models import PlanStep, SuccessCriterion
from cyborg.services.base import BaseService
from cyborg.services.context_builder import ContextBuilder, ContextScope


logger = logging.getLogger(__name__)


class OpenClawReasoningService(BaseService):
    """
    All LLM reasoning goes through OpenClaw.

    Cyborg builds context → OpenClaw does reasoning → Cyborg parses result
    """

    # Default timeouts for different reasoning types (seconds)
    TIMEOUT_PLAN = 60
    TIMEOUT_EVALUATION = 60
    TIMEOUT_REFINEMENT = 90
    TIMEOUT_LEARNING = 75
    TIMEOUT_DEFAULT = 60

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
            timeout=self.TIMEOUT_PLAN,
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
            timeout=self.TIMEOUT_EVALUATION,
        )

        return self._parse_evaluation_response(response)

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
            timeout=self.TIMEOUT_REFINEMENT,
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
            timeout=self.TIMEOUT_LEARNING,
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
        if project_links:
            project_context = await self.context_builder.build_project_context(
                project_id=project_links[0]["project_id"],
                scope=ContextScope.MINIMAL,
                focus_reasoning="task_planning",
            )

        prompt = self._build_task_plan_prompt(dict(task), project_context)

        response = await self._call_openclaw(
            prompt=prompt,
            response_format="text",
            timeout=self.TIMEOUT_DEFAULT,
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
            timeout=self.TIMEOUT_EVALUATION,
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
            timeout=self.TIMEOUT_PLAN,
        )

        return self._parse_follow_up_tasks_response(response)

    async def _call_openclaw(
        self,
        prompt: str,
        response_format: str = "text",
        timeout: int = 30,
        session_key: str | None = None,
    ) -> str:
        """
        Call OpenClaw gateway for reasoning.

        Uses a separate internal session for reasoning (not user-facing).
        """

        # Use a dedicated reasoning session
        reasoning_session = session_key or "cyborg:reasoning"

        if not self.openclaw_service.is_configured():
            raise RuntimeError("OpenClaw reasoning is not configured")

        # Build gateway params
        params = {
            "message": prompt,
            "deliver": False,  # Not delivering to a user
            "sessionKey": reasoning_session,
            "thinking": "on",
            "timeout": timeout * 1000,
            "idempotencyKey": str(uuid4()),
        }

        # Add response format hint
        if response_format == "json":
            params["message"] += "\n\nIMPORTANT: Respond with valid JSON only. No markdown formatting, no code blocks, no explanation outside the JSON."

        # Call gateway
        try:
            response = await self.openclaw_service._send_gateway_request(
                method="agent",
                params=params,
                expect_final=True,
                timeout_seconds=timeout,
            )

            return self._extract_response_text(response)

        except Exception as e:
            # Log and raise
            logger.error("OpenClaw reasoning call failed: %s", e)
            raise RuntimeError(f"OpenClaw reasoning failed: {e}") from e

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
            result = trigger_task.get("result", "")[:200]
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
            "Full Journal:",
        ]

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
