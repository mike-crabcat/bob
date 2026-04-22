"""HTTP routes for project management."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status

from cyborg.dependencies import get_project_execution_service, get_project_service, get_project_spec_service, get_source_discovery_service
from cyborg.models import (
    ProjectCloseRequest,
    ProjectCreate,
    ProjectJournalEntryCreate,
    ProjectJournalEntryResponse,
    ProjectResponse,
    ProjectState,
    ProjectUpdate,
    SourceOutputItem,
    SourceProjectResponse,
    TaskResponse,
)
from cyborg.services.project_execution_service import ProjectExecutionService
from cyborg.services.project_spec_service import ProjectSpecService
from cyborg.services.project_service import ProjectService
from cyborg.services.source_discovery_service import SourceDiscoveryService


router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


@router.post("/doctor", response_model=dict[str, Any])
async def doctor(
    fix: bool = False,
    execution_service: ProjectExecutionService = Depends(get_project_execution_service),
) -> dict[str, Any]:
    """Diagnose project health problems and optionally fix them.

    Query param `fix=true` to apply fixes (e.g., bootstrap stuck projects).
    """
    problems = await execution_service.diagnose()
    fixes: list[dict[str, Any]] = []

    if fix:
        for problem in problems:
            if problem["problem"] == "active_with_no_tasks":
                try:
                    result = await execution_service.bootstrap_stuck_project(problem["project_id"])
                    fixes.append({**problem, **result})
                except Exception as exc:
                    fixes.append({**problem, "action": "error", "error": str(exc)})
            elif problem["problem"] == "blocked_task_without_approval":
                try:
                    result = await execution_service.create_missing_approval(problem["task_id"])
                    fixes.append({**problem, **result})
                except Exception as exc:
                    fixes.append({**problem, "action": "error", "error": str(exc)})
            elif problem["problem"] == "obsolete_approval":
                try:
                    result = await execution_service.cancel_obsolete_approval(problem["approval_id"])
                    fixes.append({**problem, **result})
                except Exception as exc:
                    fixes.append({**problem, "action": "error", "error": str(exc)})
            elif problem["problem"] == "duplicate_pending_approvals":
                try:
                    cancel_ids = problem.get("cancel_approval_ids", [])
                    for aid in cancel_ids:
                        await execution_service.cancel_obsolete_approval(aid)
                    fixes.append({**problem, "action": "cancelled_duplicates", "cancelled_count": len(cancel_ids)})
                except Exception as exc:
                    fixes.append({**problem, "action": "error", "error": str(exc)})

    return {"problems": problems, "fixes": fixes}


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    state: ProjectState | None = None,
    service: ProjectService = Depends(get_project_service),
) -> list[ProjectResponse]:
    return await service.list_projects(state=state)


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    background_tasks: BackgroundTasks,
    service: ProjectService = Depends(get_project_service),
) -> ProjectResponse:
    project = await service.create_project(payload, defer_effects=True)
    background_tasks.add_task(
        service._post_create_background_effects,
        str(project.id),
        has_spec=service._build_spec_payload(
            aim=payload.aim,
            method=payload.method,
            plan=payload.plan,
            success_criteria=payload.success_criteria,
        ) is not None,
        initial_state=payload.state,
        payload=payload,
    )
    return project


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: UUID, service: ProjectService = Depends(get_project_service)) -> ProjectResponse:
    return await service.get_project(str(project_id))


@router.put("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    background_tasks: BackgroundTasks,
    service: ProjectService = Depends(get_project_service),
    spec_service: ProjectSpecService = Depends(get_project_spec_service),
) -> ProjectResponse:
    has_spec_fields = any(v is not None for v in (payload.aim, payload.method, payload.plan, payload.success_criteria))
    result = await service.update_project(str(project_id), payload, defer_plan_generation=True)
    if has_spec_fields and payload.plan is None:
        background_tasks.add_task(spec_service.generate_plan_if_needed, str(project_id))
    return result


@router.post("/{project_id}/pause", response_model=ProjectResponse)
async def pause_project(project_id: UUID, service: ProjectService = Depends(get_project_service)) -> ProjectResponse:
    return await service.pause_project(str(project_id))


@router.post("/{project_id}/resume", response_model=ProjectResponse)
async def resume_project(
    project_id: UUID,
    service: ProjectService = Depends(get_project_service),
    background_tasks: BackgroundTasks = None,
) -> ProjectResponse:
    response = await service.resume_project(str(project_id))
    if background_tasks is not None:
        background_tasks.add_task(service.resume_project_reasoning, str(project_id))
    return response


@router.post("/{project_id}/mute", response_model=ProjectResponse)
async def mute_project(project_id: UUID, service: ProjectService = Depends(get_project_service)) -> ProjectResponse:
    return await service.mute_project(str(project_id))


@router.post("/{project_id}/unmute", response_model=ProjectResponse)
async def unmute_project(project_id: UUID, service: ProjectService = Depends(get_project_service)) -> ProjectResponse:
    return await service.unmute_project(str(project_id))


@router.post("/{project_id}/close", response_model=ProjectResponse)
async def close_project(
    project_id: UUID,
    payload: ProjectCloseRequest,
    service: ProjectService = Depends(get_project_service),
) -> ProjectResponse:
    return await service.close_project(str(project_id), payload)


@router.get("/{project_id}/journal", response_model=list[ProjectJournalEntryResponse])
async def list_journal(
    project_id: UUID,
    service: ProjectService = Depends(get_project_service),
) -> list[ProjectJournalEntryResponse]:
    return await service.list_journal(str(project_id))


@router.post("/{project_id}/journal", response_model=ProjectJournalEntryResponse, status_code=status.HTTP_201_CREATED)
async def add_journal_entry(
    project_id: UUID,
    payload: ProjectJournalEntryCreate,
    service: ProjectService = Depends(get_project_service),
) -> ProjectJournalEntryResponse:
    return await service.add_journal_entry(str(project_id), payload)


@router.get("/{project_id}/tasks", response_model=list[TaskResponse])
async def list_project_tasks(
    project_id: UUID,
    service: ProjectService = Depends(get_project_service),
) -> list[TaskResponse]:
    return await service.list_project_tasks(str(project_id))


@router.post("/{project_id}/evaluate", response_model=ProjectResponse | None)
async def evaluate_project_completion(
    project_id: UUID,
    execution_service: ProjectExecutionService = Depends(get_project_execution_service),
) -> ProjectResponse | None:
    """Evaluate success criteria and auto-complete project if all criteria met.

    Returns the completed project if auto-completed, None otherwise.
    """
    return await execution_service.evaluate_and_complete(str(project_id))


@router.post("/{project_id}/decide-next", response_model=ProjectResponse)
async def decide_next(
    project_id: UUID,
    payload: dict[str, Any],
    execution_service: ProjectExecutionService = Depends(get_project_execution_service),
) -> ProjectResponse:
    """Submit an async next-action decision from reasoning.

    Called by the AI agent after receiving the next-action prompt. Must include
    a valid one-time password from the prompt.
    """
    return await execution_service.verify_decide_next(str(project_id), payload)


# --- Source project endpoints ---


@router.get("/{project_id}/sources", response_model=list[SourceProjectResponse])
async def list_sources(
    project_id: UUID,
    service: SourceDiscoveryService = Depends(get_source_discovery_service),
) -> list[SourceProjectResponse]:
    """List all source projects linked to this derived project."""
    return await service.get_sources(str(project_id))


@router.post("/{project_id}/sources", response_model=list[SourceProjectResponse], status_code=status.HTTP_201_CREATED)
async def link_sources(
    project_id: UUID,
    payload: dict[str, Any],
    service: SourceDiscoveryService = Depends(get_source_discovery_service),
) -> list[SourceProjectResponse]:
    """Link one or more source projects to this derived project."""
    source_ids = [str(sid) for sid in payload.get("source_project_ids", [])]
    result = await service.link_sources(str(project_id), source_ids)
    await service.scan_source_outputs(str(project_id))
    return result


@router.delete("/{project_id}/sources/{source_project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_source(
    project_id: UUID,
    source_project_id: UUID,
    service: SourceDiscoveryService = Depends(get_source_discovery_service),
) -> Response:
    """Remove a source project link."""
    await service.unlink_sources(str(project_id), [str(source_project_id)])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{project_id}/sources/scan", response_model=list[SourceOutputItem])
async def scan_source_outputs(
    project_id: UUID,
    service: SourceDiscoveryService = Depends(get_source_discovery_service),
) -> list[SourceOutputItem]:
    """Rescan all linked source projects for outputs."""
    return await service.scan_source_outputs(str(project_id))
