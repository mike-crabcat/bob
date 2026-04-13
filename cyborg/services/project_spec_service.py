"""Business logic for versioned project specifications."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from cyborg.database import Database
from cyborg.exceptions import ConflictError, NotFoundError
from cyborg.models import (
    ProjectSpecApproveRequest,
    ProjectSpecListResponse,
    ProjectSpecRejectRequest,
    ProjectSpecResponse,
    ProjectSpecStatus,
    ProjectSpecSubmitRequest,
    ProjectState,
)
from cyborg.services.base import BaseService, json_dumps, json_loads, utcnow


class ProjectSpecService(BaseService):
    """CRUD and approval workflow for project specifications."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)

    async def submit_spec(self, project_id: str, payload: ProjectSpecSubmitRequest, *, defer_plan_generation: bool = False) -> ProjectSpecResponse:
        project = await self._get_project_row(project_id)
        if project["state"] in (ProjectState.CLOSED.value, ProjectState.PAUSED.value):
            # Reopen closed project for spec revision
            now = utcnow().isoformat()
            await self.db.execute(
                "UPDATE projects SET state = ?, closed_at = NULL, conclusion = NULL, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (ProjectState.PLANNING.value, now, project_id),
            )

        now = utcnow().isoformat()
        spec_id = str(uuid4())
        version_row = await self.db.fetch_one(
            "SELECT MAX(version_number) AS max_version FROM project_specs WHERE project_id = ?",
            (project_id,),
        )
        next_version = (version_row["max_version"] or 0) + 1

        async with self.db.connection(write=True) as connection:
            await connection.execute(
                """
                INSERT INTO project_specs (
                    id, project_id, version_number, aim, method, plan, success_criteria,
                    status, feedback, created_at, approved_at, approved_by, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec_id,
                    project_id,
                    next_version,
                    payload.aim,
                    payload.method,
                    json_dumps([step.model_dump(mode="json") for step in payload.plan]) if payload.plan is not None else None,
                    json_dumps([criterion.model_dump(mode="json") for criterion in payload.success_criteria]),
                    ProjectSpecStatus.PENDING_APPROVAL.value,
                    None,
                    now,
                    None,
                    None,
                    0,
                ),
            )

            # If the project has never had an approved spec, surface the draft content on the project shell.
            if not project.get("current_spec_id"):
                await connection.execute(
                    """
                    UPDATE projects
                    SET aim = ?, method = ?, plan = ?, success_criteria = ?, updated_at = ?
                    WHERE id = ? AND deleted_at IS NULL
                    """,
                    (
                        payload.aim,
                        payload.method,
                        json_dumps([step.model_dump(mode="json") for step in payload.plan]) if payload.plan is not None else None,
                        json_dumps([criterion.model_dump(mode="json") for criterion in payload.success_criteria]),
                        now,
                        project_id,
                    ),
                )

        await self._create_approval_record(
            project_id,
            title=f"Spec v{next_version}: {payload.aim or project.get('title', 'Project spec')}",
            description=f"Project spec version {next_version} pending approval.",
            spec_data={
                "aim": payload.aim,
                "method": payload.method,
                "plan": [step.model_dump(mode="json") for step in (payload.plan or [])],
                "success_criteria": [c.model_dump(mode="json") for c in payload.success_criteria],
            },
        )

        # If no plan was provided, trigger reasoning to generate one
        if not payload.plan and not defer_plan_generation:
            spec_row = await self._get_spec_row(spec_id)
            await self._generate_plan_for_spec(project_id, spec_id, spec_row)

        return await self.get_spec(spec_id)

    async def list_specs(self, project_id: str) -> ProjectSpecListResponse:
        project = await self._get_project_row(project_id)
        rows = await self.db.fetch_all(
            """
            SELECT *
            FROM project_specs
            WHERE project_id = ?
            ORDER BY version_number DESC
            """,
            (project_id,),
        )
        specs = [ProjectSpecResponse.model_validate(self._decode_spec_row(row)) for row in rows]
        latest = rows[0] if rows else None
        return ProjectSpecListResponse(
            project_id=UUID(project_id),
            specs=specs,
            current_spec_id=UUID(project["current_spec_id"]) if project.get("current_spec_id") else None,
            latest_spec_id=UUID(latest["id"]) if latest else None,
            latest_spec_status=ProjectSpecStatus(latest["status"]) if latest else None,
        )

    async def get_spec(self, spec_id: str) -> ProjectSpecResponse:
        row = await self._get_spec_row(spec_id)
        return ProjectSpecResponse.model_validate(self._decode_spec_row(row))

    async def approve_spec(self, spec_id: str, payload: ProjectSpecApproveRequest) -> ProjectSpecResponse:
        row = await self._get_spec_row(spec_id)
        if row["status"] != ProjectSpecStatus.PENDING_APPROVAL.value:
            raise ConflictError(
                f"Cannot approve project spec '{spec_id}' with status '{row['status']}'. "
                "Spec must be in 'pending_approval' status."
            )

        plan = json_loads(row.get("plan"), [])
        if not plan:
            raise ConflictError(
                f"Cannot approve spec '{spec_id}' without a plan. "
                "The project must generate or receive a plan before approval."
            )

        now = utcnow().isoformat()
        project_id = row["project_id"]
        async with self.db.connection(write=True) as connection:
            await connection.execute(
                "UPDATE project_specs SET is_current = 0 WHERE project_id = ?",
                (project_id,),
            )
            await connection.execute(
                """
                UPDATE project_specs
                SET status = ?, approved_at = ?, approved_by = ?, is_current = 1
                WHERE id = ?
                """,
                (
                    ProjectSpecStatus.APPROVED.value,
                    now,
                    payload.approver,
                    spec_id,
                ),
            )
            # Update project with spec content (plan may be NULL, [], or populated — all valid)
            await connection.execute(
                """
                UPDATE projects
                SET current_spec_id = ?, aim = ?, method = ?, plan = ?, success_criteria = ?, updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (
                    spec_id,
                    row["aim"],
                    row["method"],
                    row["plan"],
                    row["success_criteria"],
                    now,
                    project_id,
                ),
            )

        project = await self._get_project_row(project_id)

        if project["state"] in (ProjectState.PLANNING.value, ProjectState.PAUSED.value, ProjectState.ACTIVE.value):
            from cyborg.services.project_execution_service import ProjectExecutionService
            execution_service = ProjectExecutionService(self.db)
            # Clean up old auto-created tasks before re-planning (also PLANNING — reopened closed projects)
            if project["state"] in (ProjectState.PLANNING.value, ProjectState.PAUSED.value, ProjectState.ACTIVE.value):
                await execution_service.cleanup_old_plan_tasks(project_id)
            await execution_service.start_project_execution(project_id)

        return await self.get_spec(spec_id)

    async def reject_spec(self, spec_id: str, payload: ProjectSpecRejectRequest) -> ProjectSpecResponse:
        row = await self._get_spec_row(spec_id)
        if row["status"] != ProjectSpecStatus.PENDING_APPROVAL.value:
            raise ConflictError(
                f"Cannot reject project spec '{spec_id}' with status '{row['status']}'. "
                "Spec must be in 'pending_approval' status."
            )

        async with self.db.connection(write=True) as connection:
            await connection.execute(
                """
                UPDATE project_specs
                SET status = ?, feedback = ?
                WHERE id = ?
                """,
                (
                    ProjectSpecStatus.REJECTED.value,
                    payload.feedback,
                    spec_id,
                ),
            )

        return await self.get_spec(spec_id)

    async def revise_spec_after_rejection(
        self,
        project_id: str,
        feedback: str,
        *,
        allow_aim_changes: bool = False,
        allow_criteria_changes: bool = False,
    ) -> ProjectSpecResponse | None:
        """Generate a revised spec based on feedback and submit it.

        Returns the new spec, or None if revision fails (project stays in planning
        for manual re-submission).
        """
        # Get the latest spec to use as the base
        latest = await self.db.fetch_one(
            "SELECT aim, method, plan, success_criteria FROM project_specs "
            "WHERE project_id = ? ORDER BY version_number DESC LIMIT 1",
            (project_id,),
        )
        if not latest:
            return None

        # Fetch current non-deprecated tasks so reasoning can identify obsolete ones
        task_rows = await self.db.fetch_all(
            """
            SELECT t.id, t.title, t.status, t.result
            FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ?
              AND t.deleted_at IS NULL
              AND t.status NOT IN ('deprecated', 'failed')
            ORDER BY t.created_at
            """,
            (project_id,),
        )
        current_tasks = [dict(r) for r in task_rows] if task_rows else []

        from cyborg.services.openclaw_reasoning_service import OpenClawReasoningService
        reasoning = OpenClawReasoningService(self.db)

        revised = await reasoning.revise_spec(
            aim=latest["aim"],
            method=latest.get("method"),
            success_criteria=json_loads(latest.get("success_criteria"), []),
            plan_steps=json_loads(latest.get("plan"), []),
            feedback=feedback,
            allow_aim_changes=allow_aim_changes,
            allow_criteria_changes=allow_criteria_changes,
            reference_project_id=project_id,
            current_tasks=current_tasks,
        )

        if not revised:
            return None

        # Deprecate tasks identified as obsolete by reasoning
        deprecated_ids = revised.get("deprecated_task_ids", [])
        if deprecated_ids:
            now = utcnow().isoformat()
            for tid in deprecated_ids:
                if isinstance(tid, str):
                    await self.db.execute(
                        "UPDATE tasks SET status = 'deprecated', updated_at = ? WHERE id = ? AND deleted_at IS NULL AND status NOT IN ('deprecated')",
                        (now, tid),
                    )

        # Build and submit the new spec
        from cyborg.models import (
            PlanStep,
            ProjectSpecSubmitRequest,
            SuccessCriterion,
        )

        aim = revised.get("aim", latest["aim"]) if allow_aim_changes else latest["aim"]

        raw_criteria = revised.get("success_criteria", [])
        if allow_criteria_changes and raw_criteria:
            criteria = [
                SuccessCriterion(
                    check=c.get("check", c.get("description", "")),
                    description=c.get("description", c.get("check", "")),
                )
                for c in raw_criteria if isinstance(c, dict)
            ]
        else:
            # Keep original criteria
            existing = json_loads(latest.get("success_criteria"), [])
            criteria = [SuccessCriterion(**c) for c in existing]

        raw_steps = revised.get("plan", [])
        plan = [
            PlanStep(
                title=s.get("title", ""),
                description=s.get("description", ""),
                criteria=s.get("criteria", ""),
                order=s.get("order", i),
            )
            for i, s in enumerate(raw_steps)
        ] if raw_steps else []

        method = revised.get("method", latest.get("method")) or latest.get("method")

        payload = ProjectSpecSubmitRequest(
            aim=aim,
            method=method,
            plan=plan,
            success_criteria=criteria,
        )

        return await self.submit_spec(project_id, payload, defer_plan_generation=True)


    async def ensure_project_ready_for_execution(self, project_id: str) -> None:
        project = await self._get_project_row(project_id)
        current_spec_id = project.get("current_spec_id")
        if not current_spec_id:
            raise ConflictError(
                f"Project '{project_id}' cannot start or execute without an approved project spec. "
                "Submit a spec and have it approved first."
            )

        spec = await self.db.fetch_one(
            """
            SELECT status, aim, method, success_criteria
            FROM project_specs
            WHERE id = ? AND project_id = ?
            """,
            (current_spec_id, project_id),
        )
        if spec is None or spec["status"] != ProjectSpecStatus.APPROVED.value:
            raise ConflictError(
                f"Project '{project_id}' cannot start or execute because its current spec is not approved."
            )

        criteria = json_loads(spec.get("success_criteria"), [])
        if not spec.get("aim") or not spec["aim"].strip():
            raise ConflictError(f"Project '{project_id}' cannot start or execute without an approved aim.")
        if not spec.get("method") or not spec["method"].strip():
            raise ConflictError(f"Project '{project_id}' cannot start or execute without an approved method.")
        if not criteria:
            raise ConflictError(
                f"Project '{project_id}' cannot start or execute without approved success criteria."
            )

    async def get_latest_spec_summary(self, project_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            """
            SELECT id, status, aim, method, success_criteria, feedback, approved_at, approved_by
            FROM project_specs
            WHERE project_id = ?
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (project_id,),
        )
        return self._decode_spec_row(row) if row is not None else None

    async def populate_project_spec_fields(self, row: dict[str, Any]) -> dict[str, Any]:
        latest = await self.get_latest_spec_summary(row["id"])
        row["current_spec_id"] = row.get("current_spec_id")
        row["latest_spec_id"] = latest["id"] if latest else None
        row["latest_spec_status"] = latest["status"] if latest else None
        return row

    async def _create_approval_record(
        self,
        project_id: str,
        title: str,
        description: str | None = None,
        spec_data: dict[str, Any] | None = None,
    ) -> None:
        """Create a pending approval record so the spec appears in the dashboard queue."""
        approval_id = str(uuid4())
        now = utcnow().isoformat()
        proposal_json = json_dumps(spec_data) if spec_data else None
        await self.db.execute(
            """
            INSERT INTO approvals (
                id, approval_type, entity_id, title, description,
                proposal_data, status, priority, requested_at, requested_by, created_at
            ) VALUES (?, 'project_plan', ?, ?, ?, ?, 'pending', 'normal', ?, 'system', ?)
            """,
            (approval_id, project_id, title, description, proposal_json, now, now),
        )

    async def _get_project_row(self, project_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one(
            "SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_id,),
        )
        if row is None:
            raise NotFoundError(f"Project '{project_id}' was not found")
        return row

    async def _get_spec_row(self, spec_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM project_specs WHERE id = ?", (spec_id,))
        if row is None:
            raise NotFoundError(f"Project spec '{spec_id}' was not found")
        return row

    def _decode_spec_row(self, row: dict[str, Any] | None) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("Project spec was not found")
        decoded = dict(row)
        decoded["plan"] = json_loads(decoded.get("plan"), [])
        decoded["success_criteria"] = json_loads(decoded.get("success_criteria"), [])
        decoded["is_current"] = bool(decoded.get("is_current", 0))
        return decoded

    async def generate_plan_if_needed(self, project_id: str) -> None:
        """Generate a plan for the latest spec if it doesn't already have one.

        Safe to call as a background task — no-ops when the spec already has a plan.
        """
        spec_row = await self.db.fetch_one(
            "SELECT id, aim, method, success_criteria, plan, version_number "
            "FROM project_specs WHERE project_id = ? ORDER BY version_number DESC LIMIT 1",
            (project_id,),
        )
        if spec_row is None:
            return
        existing_plan = json_loads(spec_row.get("plan"), [])
        if existing_plan:
            return
        await self._generate_plan_for_spec(project_id, spec_row["id"], dict(spec_row))

    async def _generate_plan_for_spec(
        self,
        project_id: str,
        spec_id: str,
        spec_row: dict[str, Any],
    ) -> None:
        """Ask OpenClaw to generate an execution plan and patch it into the existing spec."""
        import json as _json
        import logging

        logger = logging.getLogger(__name__)

        try:
            from cyborg.services.openclaw_reasoning_service import OpenClawReasoningService

            reasoning = OpenClawReasoningService(self.db)
            criteria = json_loads(spec_row.get("success_criteria"), [])
            criteria_texts = [
                c.get("description", c.get("check", ""))
                for c in criteria
                if isinstance(c, dict)
            ]

            generated_steps = await reasoning.generate_project_plan(
                aim=spec_row["aim"],
                method=spec_row.get("method"),
                success_criteria=criteria_texts or None,
                reference_project_id=project_id,
            )
        except Exception as e:
            logger.warning(
                "OpenClaw plan generation failed for project %s: %s. "
                "Project will remain in planning until a plan is provided.",
                project_id,
                e,
            )
            return

        if not generated_steps:
            logger.warning(
                "OpenClaw generated no plan steps for project %s. "
                "Project will remain in planning until a plan is provided.",
                project_id,
            )
            return

        plan_json = json_dumps(generated_steps)
        now = utcnow().isoformat()

        # Patch the existing spec row with the generated plan
        await self.db.execute(
            "UPDATE project_specs SET plan = ? WHERE id = ?",
            (plan_json, spec_id),
        )

        # Patch the project row so the project shell reflects the plan
        await self.db.execute(
            "UPDATE projects SET plan = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (plan_json, now, project_id),
        )

        # Update the existing approval record's proposal_data to include the plan
        await self.db.execute(
            """
            UPDATE approvals SET proposal_data = ?, title = ?, description = ?
            WHERE entity_id = ? AND approval_type = 'project_plan' AND status = 'pending'
            """,
            (
                json_dumps({
                    "aim": spec_row["aim"],
                    "method": spec_row.get("method"),
                    "plan": generated_steps,
                    "success_criteria": json_loads(spec_row.get("success_criteria"), []),
                }),
                f"Spec v{spec_row.get('version_number', 1)}: {spec_row.get('aim', 'Project spec')}",
                f"Plan generated ({len(generated_steps)} steps). Ready for approval.",
                project_id,
            ),
        )

        # Add a journal entry noting the plan was generated
        await self.db.execute(
            """
            INSERT INTO project_journal_entries (id, project_id, entry_type, content, created_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                project_id,
                "decision",
                f"OpenClaw generated an execution plan ({len(generated_steps)} steps). "
                "The spec is ready for approval.",
                now,
                json_dumps({
                    "autonomy_action": "plan_generated",
                    "spec_id": spec_id,
                    "step_count": len(generated_steps),
                }),
            ),
        )

        logger.info(
            "Generated plan for spec %s on project %s (%d steps)",
            spec_id,
            project_id,
            len(generated_steps),
        )
