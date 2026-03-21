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
from cyborg.services.notification_service import NotificationService


class ProjectSpecService(BaseService):
    """CRUD and approval workflow for project specifications."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)

    async def _sync_project_notifications(self, project_id: str, *, immediate: bool = False) -> None:
        await NotificationService(self.db).sync_project_state(project_id, immediate=immediate)

    async def submit_spec(self, project_id: str, payload: ProjectSpecSubmitRequest) -> ProjectSpecResponse:
        project = await self._get_project_row(project_id)
        if project["state"] == ProjectState.CLOSED.value:
            raise ConflictError(f"Cannot submit a spec for closed project '{project_id}'")

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
                    json_dumps([step.model_dump(mode="json") for step in payload.plan]) if payload.plan else None,
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
                    SET aim = ?, method = ?, plan = ?, success_criteria = ?
                    WHERE id = ? AND deleted_at IS NULL
                    """,
                    (
                        payload.aim,
                        payload.method,
                        json_dumps([step.model_dump(mode="json") for step in payload.plan]) if payload.plan else None,
                        json_dumps([criterion.model_dump(mode="json") for criterion in payload.success_criteria]),
                        project_id,
                    ),
                )

        await self._sync_project_notifications(project_id, immediate=True)
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
            await connection.execute(
                """
                UPDATE projects
                SET current_spec_id = ?, aim = ?, method = ?, plan = ?, success_criteria = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (
                    spec_id,
                    row["aim"],
                    row["method"],
                    row["plan"],
                    row["success_criteria"],
                    project_id,
                ),
            )

        await self._sync_project_notifications(project_id, immediate=False)
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

        await self._sync_project_notifications(row["project_id"], immediate=True)
        return await self.get_spec(spec_id)

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
