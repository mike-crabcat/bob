"""Business logic for plan versioning."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from cyborg.database import Database
from cyborg.exceptions import ConflictError, NotFoundError
from cyborg.models import (
    PlanApproveRequest,
    PlanListResponse,
    PlanRejectRequest,
    PlanResponse,
    PlanStatus,
    PlanSubmitRequest,
    TaskStatus,
)
from cyborg.services.base import BaseService, utcnow
from cyborg.services.notification_service import NotificationService
from cyborg.services.task_service import TaskService


class PlanService(BaseService):
    """CRUD and lifecycle operations for task plans."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)

    async def _sync_task_notifications(self, task_id: str, *, immediate: bool = False) -> None:
        await NotificationService(self.db).sync_task_state(task_id, immediate=immediate)

    async def create_plan(self, task_id: str, payload: PlanSubmitRequest) -> PlanResponse:
        """Create a new plan version for a task.
        
        The plan is created with status 'pending_approval'.
        The task must be in 'planning' status to submit a plan.
        """
        # Verify task exists and is in planning status
        task_row = await self._get_task_row(task_id)
        if task_row["status"] != TaskStatus.PLANNING.value:
            raise ConflictError(
                f"Cannot submit plan for task '{task_id}' with status '{task_row['status']}'. "
                "Task must be in 'planning' status."
            )

        now = utcnow()
        plan_id = str(uuid4())

        # Get the next version number for this task
        version_row = await self.db.fetch_one(
            "SELECT MAX(version_number) as max_version FROM plans WHERE task_id = ?",
            (task_id,),
        )
        next_version = (version_row["max_version"] or 0) + 1

        async with self.db.connection(write=True) as connection:
            # Planning means there is no accepted executable plan yet.
            await connection.execute(
                "UPDATE plans SET is_current = 0 WHERE task_id = ?",
                (task_id,),
            )
            await connection.execute(
                "UPDATE tasks SET plan = ?, current_plan_id = NULL, updated_at = ? WHERE id = ?",
                (payload.content, now.isoformat(), task_id),
            )

            # Create the new plan
            await connection.execute(
                """
                INSERT INTO plans (
                    id, task_id, version_number, content, status,
                    feedback, created_at, approved_at, approved_by, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    task_id,
                    next_version,
                    payload.content,
                    PlanStatus.PENDING_APPROVAL.value,
                    None,
                    now.isoformat(),
                    None,
                    None,
                    0,
                ),
            )

        await self._sync_task_notifications(task_id, immediate=True)
        return await self.get_plan(plan_id)

    async def list_plans(self, task_id: str) -> PlanListResponse:
        """List all plan versions for a task."""
        # Verify task exists
        await self._get_task_row(task_id)

        rows = await self.db.fetch_all(
            """
            SELECT * FROM plans
            WHERE task_id = ?
            ORDER BY version_number DESC
            """,
            (task_id,),
        )

        # Get the task's current_plan_id
        task_row = await self._get_task_row(task_id)
        current_plan_id = task_row.get("current_plan_id")

        plans = []
        for row in rows:
            decoded = self._decode_plan_row(row)
            plans.append(PlanResponse.model_validate(decoded))

        return PlanListResponse(
            task_id=UUID(task_id),
            plans=plans,
            current_plan_id=UUID(current_plan_id) if current_plan_id else None,
        )

    async def get_plan(self, plan_id: str) -> PlanResponse:
        """Get a specific plan by ID."""
        row = await self._get_plan_row(plan_id)
        decoded = self._decode_plan_row(row)
        return PlanResponse.model_validate(decoded)

    async def approve_plan(self, plan_id: str, payload: PlanApproveRequest) -> PlanResponse:
        """Approve a plan.
        
        The plan status changes to 'approved', becomes the current plan for the task,
        and the task moves to 'pending'.
        """
        row = await self._get_plan_row(plan_id)

        if row["status"] != PlanStatus.PENDING_APPROVAL.value:
            raise ConflictError(
                f"Cannot approve plan '{plan_id}' with status '{row['status']}'. "
                "Plan must be in 'pending_approval' status."
            )

        now = utcnow()
        task_id = row["task_id"]
        task_row = await self._get_task_row(task_id)
        dependency_ready = await self._dependency_is_satisfied(task_row)
        next_status = TaskStatus.PENDING if dependency_ready else TaskStatus.BLOCKED
        blocked_reason = None if dependency_ready else TaskService.dependency_blocked_reason(task_row["parent_id"])
        blocked_resume_instructions = (
            None if dependency_ready else TaskService.dependency_blocked_resume_instructions(task_row["parent_id"])
        )

        async with self.db.connection(write=True) as connection:
            # Mark all other plans for this task as not current
            await connection.execute(
                "UPDATE plans SET is_current = 0 WHERE task_id = ?",
                (task_id,),
            )

            # Approve this plan and mark as current
            await connection.execute(
                """
                UPDATE plans
                SET status = ?, approved_at = ?, approved_by = ?, is_current = 1
                WHERE id = ?
                """,
                (
                    PlanStatus.APPROVED.value,
                    now.isoformat(),
                    payload.approver,
                    plan_id,
                ),
            )

            # Update the task's current_plan_id and mark it ready to start.
            await connection.execute(
                """
                UPDATE tasks
                SET current_plan_id = ?, status = ?, updated_at = ?, blocked_reason = ?, blocked_resume_instructions = ?
                WHERE id = ?
                """,
                (
                    plan_id,
                    next_status.value,
                    now.isoformat(),
                    blocked_reason,
                    blocked_resume_instructions,
                    task_id,
                ),
            )

        await self._sync_task_notifications(task_id, immediate=False)
        return await self.get_plan(plan_id)

    async def reject_plan(self, plan_id: str, payload: PlanRejectRequest) -> PlanResponse:
        """Reject a plan with feedback.
        
        The plan status changes to 'rejected' and the task remains in 'planning' status.
        A new plan must be submitted before the task can become active.
        """
        row = await self._get_plan_row(plan_id)

        if row["status"] != PlanStatus.PENDING_APPROVAL.value:
            raise ConflictError(
                f"Cannot reject plan '{plan_id}' with status '{row['status']}'. "
                "Plan must be in 'pending_approval' status."
            )

        async with self.db.connection(write=True) as connection:
            await connection.execute(
                """
                UPDATE plans
                SET status = ?, feedback = ?
                WHERE id = ?
                """,
                (
                    PlanStatus.REJECTED.value,
                    payload.feedback,
                    plan_id,
                ),
            )
            await connection.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                (utcnow().isoformat(), row["task_id"]),
            )

        await self._sync_task_notifications(row["task_id"], immediate=True)
        return await self.get_plan(plan_id)

    async def _get_task_row(self, task_id: str) -> dict[str, Any]:
        """Get a task row or raise NotFoundError."""
        row = await self.db.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (task_id,),
        )
        if row is None:
            raise NotFoundError(f"Task '{task_id}' was not found")
        return row

    async def _get_plan_row(self, plan_id: str) -> dict[str, Any]:
        """Get a plan row or raise NotFoundError."""
        row = await self.db.fetch_one(
            "SELECT * FROM plans WHERE id = ?",
            (plan_id,),
        )
        if row is None:
            raise NotFoundError(f"Plan '{plan_id}' was not found")
        return row

    async def _dependency_is_satisfied(self, task_row: dict[str, Any]) -> bool:
        parent_id = task_row.get("parent_id")
        if not parent_id:
            return True
        parent = await self.db.fetch_one(
            "SELECT status, deleted_at FROM tasks WHERE id = ?",
            (parent_id,),
        )
        if parent is None or parent.get("deleted_at") is not None:
            return True
        return parent["status"] == TaskStatus.COMPLETED.value

    def _decode_plan_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Decode a plan row for model validation."""
        return {
            "id": row["id"],
            "task_id": UUID(row["task_id"]),
            "version_number": row["version_number"],
            "content": row["content"],
            "status": row["status"],
            "feedback": row["feedback"],
            "is_current": bool(row["is_current"]),
            "created_at": row["created_at"],
            "approved_at": row["approved_at"],
            "approved_by": row["approved_by"],
        }
