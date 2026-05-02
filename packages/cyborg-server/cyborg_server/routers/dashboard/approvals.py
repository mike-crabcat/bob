"""Dashboard approval routes."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import HTMLResponse

from cyborg_server.context import AppContext
from cyborg_server.dependencies import get_app_context

from ._helpers import (
    _approval_review_href,
    _format_duration_minutes,
    _get_project_id_for_task,
    _get_settings,
    _render_template,
)

router = APIRouter()


@router.get("/approvals", response_class=HTMLResponse)
async def approvals(
    request: Request,
    ctx: AppContext = Depends(get_app_context),
    type: str | None = None,
) -> Response:
    settings = _get_settings()
    db = ctx.db

    query = """
        SELECT a.*,
               CASE
                   WHEN a.priority = 'urgent' THEN 1
                   WHEN a.priority = 'high' THEN 2
                   WHEN a.priority = 'normal' THEN 3
                   ELSE 4
               END as priority_order
        FROM approvals a
        WHERE a.status = 'pending'
        """
    params = []

    if type:
        query += " AND a.approval_type = ?"
        params.append(type)

    query += " ORDER BY priority_order, a.requested_at ASC"

    approvals_data = await db.fetch_all(query, tuple(params))

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    stats = await db.fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM approvals WHERE status = 'pending') as pending,
            (SELECT COUNT(*) FROM approvals WHERE status = 'approved' AND reviewed_at >= ?) as approved_today,
            (SELECT COUNT(*) FROM approvals WHERE status = 'rejected' AND reviewed_at >= ?) as rejected_today
        """,
        (today_start, today_start),
    )
    avg_response = await db.fetch_one(
        """
        SELECT AVG((julianday(reviewed_at) - julianday(requested_at)) * 24 * 60) as avg_minutes
        FROM approvals
        WHERE status IN ('approved', 'rejected')
          AND reviewed_at IS NOT NULL
          AND requested_at IS NOT NULL
        """,
    )

    approvals = []
    for a in approvals_data:
        proposal_data = None
        proposal_preview = None
        if a.get("proposal_data"):
            try:
                proposal_data = json.loads(a["proposal_data"])
                proposal_preview = json.dumps(proposal_data, indent=2)[:500]
            except (json.JSONDecodeError, TypeError):
                proposal_preview = a["proposal_data"][:500] if a["proposal_data"] else None

        input_schema = None
        if a.get("input_schema"):
            try:
                input_schema = json.loads(a["input_schema"])
            except (json.JSONDecodeError, TypeError):
                pass

        project_id = None
        approval_metadata = None
        if a.get("metadata"):
            try:
                approval_metadata = json.loads(a["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass

        if a["approval_type"] == "task_input" and a.get("entity_id"):
            if approval_metadata and approval_metadata.get("entity_kind") == "project":
                project_id = a["entity_id"]
            else:
                project_id = await _get_project_id_for_task(db, a["entity_id"])

        if input_schema and project_id and input_schema.get("type") == "multi_choice":
            for option in input_schema.get("options", []):
                if option.get("image_url"):
                    option["image_url"] = f"/dashboard/projects/{project_id}/files/{option['image_url']}"
                if option.get("audio_url"):
                    option["audio_url"] = f"/dashboard/projects/{project_id}/files/{option['audio_url']}"

        approvals.append({
            "id": a["id"],
            "approval_type": a["approval_type"],
            "type_label": a["approval_type"].replace("_", " ").title(),
            "entity_id": a.get("entity_id"),
            "title": a["title"],
            "description": a.get("description", ""),
            "priority": a.get("priority", "normal"),
            "requested_at": a.get("requested_at"),
            "requested_by": a.get("requested_by"),
            "proposal": proposal_data,
            "proposal_preview": proposal_preview,
            "input_schema": input_schema,
            "project_id": project_id,
            "review_href": _approval_review_href(a["approval_type"], a.get("entity_id"), approval_metadata),
        })

    return _render_template(
        "dashboard/approvals.html",
        request,
        {
            "version": settings.version,
            "approvals": approvals,
            "filter_type": type,
            "pending_count": stats["pending"] if stats else 0,
            "approved_today": stats["approved_today"] if stats else 0,
            "rejected_today": stats["rejected_today"] if stats else 0,
            "avg_response_time": _format_duration_minutes(avg_response["avg_minutes"] if avg_response else None),
        },
    )


@router.post("/approve/{approval_id}", response_class=HTMLResponse)
async def approve_approval(
    approval_id: str,
    request: Request,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    db = ctx.db
    approval = await db.fetch_one(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    )
    if not approval:
        return Response(content="Approval not found", status_code=404)

    entity_id = approval["entity_id"]
    approval_type = approval["approval_type"]

    if approval_type == "project_plan" and entity_id:
        from cyborg_server.services.project_spec_service import ProjectSpecService
        from cyborg_server.models import ProjectSpecApproveRequest

        spec_service = ProjectSpecService(ctx)
        pending_spec = await db.fetch_one(
            """
            SELECT id FROM project_specs
            WHERE project_id = ? AND status = 'pending_approval'
            ORDER BY version_number DESC LIMIT 1
            """,
            (entity_id,),
        )
        if pending_spec:
            approve_payload = ProjectSpecApproveRequest(approver="dashboard_user")
            await spec_service.approve_spec(str(pending_spec["id"]), approve_payload)
        else:
            from cyborg_server.models import ProjectState
            project = await db.fetch_one(
                "SELECT state FROM projects WHERE id = ? AND deleted_at IS NULL",
                (entity_id,),
            )
            if project and project["state"] in (
                ProjectState.PLANNING.value,
                ProjectState.PAUSED.value,
                ProjectState.ACTIVE.value,
            ):
                from cyborg_server.services.project_execution_service import ProjectExecutionService
                execution_service = ProjectExecutionService(ctx)
                if project["state"] in (ProjectState.PLANNING.value, ProjectState.PAUSED.value, ProjectState.ACTIVE.value):
                    await execution_service.cleanup_old_plan_tasks(entity_id)
                await execution_service.start_project_execution(entity_id)

    if approval_type == "task_input" and entity_id:
        approval_metadata = json.loads(approval.get("metadata") or "{}")
        is_project_block = approval_metadata.get("entity_kind") == "project"

        if is_project_block:
            from cyborg_server.services.project_service import ProjectService
            from cyborg_server.services.project_execution_service import ProjectExecutionService
            from cyborg_server.models import JournalEntryType

            execution_service = ProjectExecutionService(ctx)
            await execution_service._add_journal_entry(
                entity_id,
                JournalEntryType.DECISION,
                f"User approved project block (resuming). Block reason: {approval.get('description', '')}",
                {"action": "block_approved", "approval_id": approval_id},
            )

            project_service = ProjectService(ctx)
            try:
                await project_service.resume_project(entity_id)
                try:
                    await project_service.resume_project_reasoning(entity_id, resumed_from_block=True)
                except Exception:
                    pass
            except Exception:
                pass
        else:
            from cyborg_server.services.task_service import TaskService
            from cyborg_server.models import TaskUnblockRequest

            task_service = TaskService(ctx)
            try:
                await task_service.unblock_task(
                    entity_id,
                    TaskUnblockRequest(notes="Unblocked via dashboard"),
                )
            except Exception:
                pass

    await db.execute(
        """
        UPDATE approvals
        SET status = 'approved',
            reviewed_at = ?,
            reviewed_by = 'dashboard_user'
        WHERE id = ?
        """,
        (datetime.now(timezone.utc).isoformat(), approval_id),
    )

    next_url = request.query_params.get("next")
    if next_url:
        return Response(status_code=303, headers={"Location": next_url})

    return Response(content="", status_code=200)


@router.post("/reject/{approval_id}", response_class=HTMLResponse)
async def reject_approval(
    approval_id: str,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    db = ctx.db
    approval = await db.fetch_one(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    )
    if not approval:
        return Response(content="Approval not found", status_code=404)

    entity_id = approval["entity_id"]
    approval_type = approval["approval_type"]

    if approval_type == "project_plan" and entity_id:
        from cyborg_server.services.project_spec_service import ProjectSpecService
        from cyborg_server.models import ProjectSpecRejectRequest

        spec_service = ProjectSpecService(ctx)
        pending_spec = await db.fetch_one(
            """
            SELECT id FROM project_specs
            WHERE project_id = ? AND status = 'pending_approval'
            ORDER BY version_number DESC LIMIT 1
            """,
            (entity_id,),
        )
        if pending_spec:
            reject_payload = ProjectSpecRejectRequest(feedback="Rejected via dashboard")
            await spec_service.reject_spec(str(pending_spec["id"]), reject_payload)

    if approval_type == "task_input" and entity_id:
        approval_metadata = json.loads(approval.get("metadata") or "{}")
        is_project_block = approval_metadata.get("entity_kind") == "project"

        if is_project_block:
            from cyborg_server.services.project_service import ProjectService

            project_service = ProjectService(ctx)
            try:
                await project_service.close_project(
                    entity_id,
                    conclusion="User declined to provide input via dashboard",
                )
            except Exception:
                pass
        else:
            from cyborg_server.services.task_service import TaskService
            from cyborg_server.models import TaskUnblockRequest

            task_service = TaskService(ctx)
            try:
                await task_service.unblock_task(
                    entity_id,
                    TaskUnblockRequest(notes="User declined to provide input via dashboard"),
                )
            except Exception:
                pass

    await db.execute(
        """
        UPDATE approvals
        SET status = 'rejected',
            reviewed_at = ?,
            reviewed_by = 'dashboard_user'
        WHERE id = ?
        """,
        (datetime.now(timezone.utc).isoformat(), approval_id),
    )

    return Response(content="", status_code=200)


@router.post("/specs/{spec_id}/reject-and-revise", response_class=HTMLResponse)
async def reject_and_revise_spec(
    spec_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    from cyborg_server.models import ProjectSpecRejectRequest
    from cyborg_server.services.project_spec_service import ProjectSpecService

    form = await request.form()
    feedback = str(form.get("feedback", "")).strip()
    allow_aim = form.get("allow_aim_changes") is not None
    allow_criteria = form.get("allow_criteria_changes") is not None

    if not feedback:
        return Response(content="Feedback is required", status_code=400)

    spec_service = ProjectSpecService(ctx)
    db = ctx.db

    reject_payload = ProjectSpecRejectRequest(feedback=feedback)
    await spec_service.reject_spec(spec_id, reject_payload)

    spec_row = await db.fetch_one("SELECT project_id FROM project_specs WHERE id = ?", (spec_id,))
    project_id = spec_row["project_id"] if spec_row else None
    if project_id:
        await db.execute(
            """
            UPDATE approvals SET status = 'rejected', reviewed_at = ?, reviewed_by = 'dashboard_user'
            WHERE entity_id = ? AND approval_type = 'project_plan' AND status = 'pending'
            """,
            (datetime.now(timezone.utc).isoformat(), project_id),
        )

        async def _run_revision():
            try:
                await spec_service.revise_spec_after_rejection(
                    project_id,
                    feedback,
                    allow_aim_changes=allow_aim,
                    allow_criteria_changes=allow_criteria,
                )
            except Exception:
                pass

        background_tasks.add_task(_run_revision)

    redirect_id = project_id or ""
    return Response(
        status_code=303,
        headers={"Location": f"/dashboard/projects/{redirect_id}"},
    )


@router.post("/approve/{approval_id}/input", response_class=HTMLResponse)
async def resolve_task_input(
    approval_id: str,
    request: Request,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    import logging

    db = ctx.db
    approval = await db.fetch_one(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    )
    if not approval:
        return Response(content="Approval not found", status_code=404)

    if approval["approval_type"] != "task_input":
        return Response(content="Not a task input approval", status_code=400)

    entity_id = approval["entity_id"]

    form = await request.form()
    response_value = form.get("response", "")
    if isinstance(response_value, str) and not response_value.strip():
        return Response(content="Response is required", status_code=400)

    input_schema_raw = approval.get("input_schema")
    input_prompt = ""
    if input_schema_raw:
        try:
            input_schema = json.loads(input_schema_raw)
            input_prompt = input_schema.get("prompt", "")
            if input_schema.get("type") == "multi_choice" and input_schema.get("allow_multiple"):
                values = form.getlist("response")
                response_value = [v.strip() for v in values if v.strip()]
        except (json.JSONDecodeError, TypeError):
            pass

    now_iso = datetime.now(timezone.utc).isoformat()
    response_json = json.dumps(response_value if isinstance(response_value, list) else response_value.strip())

    await db.execute(
        """
        UPDATE approvals
        SET status = 'approved',
            input_response = ?,
            reviewed_at = ?,
            reviewed_by = 'dashboard_user'
        WHERE id = ?
        """,
        (response_json, now_iso, approval_id),
    )

    approval_metadata = json.loads(approval.get("metadata") or "{}")
    is_project_block = approval_metadata.get("entity_kind") == "project"

    response_summary = response_value if isinstance(response_value, str) else ", ".join(response_value)

    if is_project_block:
        from cyborg_server.services.project_service import ProjectService
        from cyborg_server.services.project_execution_service import ProjectExecutionService
        from cyborg_server.models import JournalEntryType

        execution_service = ProjectExecutionService(ctx)
        await execution_service._add_journal_entry(
            entity_id,
            JournalEntryType.DECISION,
            f"User response to block: {response_summary}",
            {"user_response": response_summary, "approval_id": approval_id},
        )

        project_service = ProjectService(ctx)
        try:
            await project_service.resume_project(entity_id)
        except Exception:
            pass
        try:
            await project_service.resume_project_reasoning(entity_id, resumed_from_block=True)
        except Exception:
            pass
    else:
        from cyborg_server.services.task_service import TaskService
        from cyborg_server.models import TaskUnblockRequest

        task_service = TaskService(ctx)
        await task_service.unblock_task(
            entity_id,
            TaskUnblockRequest(notes=f"User input received: {response_summary}"),
        )

        try:
            from cyborg_server.services.notification_service import NotificationService
            notification_service = NotificationService(ctx)
            await notification_service.create_task_input_response_notification(
                entity_id,
                response_value if isinstance(response_value, list) else response_value.strip(),
                input_prompt,
                approval_id,
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to dispatch task_input_response notification for approval %s", approval_id,
            )

    return Response(content="", status_code=200)
