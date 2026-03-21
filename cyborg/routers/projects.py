"""HTTP routes for project management."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from cyborg.dependencies import get_project_execution_service, get_project_service, get_task_service
from cyborg.models import (
    ProjectCloseRequest,
    ProjectCreate,
    ProjectJournalEntryCreate,
    ProjectJournalEntryResponse,
    ProjectResponse,
    ProjectState,
    ProjectUpdate,
    TaskCreate,
    TaskResponse,
)
from cyborg.services.project_execution_service import ProjectExecutionService
from cyborg.services.project_service import ProjectService
from cyborg.services.task_service import TaskService


router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    state: ProjectState | None = None,
    service: ProjectService = Depends(get_project_service),
) -> list[ProjectResponse]:
    return await service.list_projects(state=state)


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    service: ProjectService = Depends(get_project_service),
) -> ProjectResponse:
    return await service.create_project(payload)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: UUID, service: ProjectService = Depends(get_project_service)) -> ProjectResponse:
    return await service.get_project(str(project_id))


@router.put("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    service: ProjectService = Depends(get_project_service),
) -> ProjectResponse:
    return await service.update_project(str(project_id), payload)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: UUID, service: ProjectService = Depends(get_project_service)) -> Response:
    await service.delete_project(str(project_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{project_id}/start", response_model=ProjectResponse)
async def start_project(project_id: UUID, service: ProjectService = Depends(get_project_service)) -> ProjectResponse:
    project = await service.start_project(str(project_id))
    await service._update_summary_md(project)
    return project


@router.post("/{project_id}/pause", response_model=ProjectResponse)
async def pause_project(project_id: UUID, service: ProjectService = Depends(get_project_service)) -> ProjectResponse:
    project = await service.pause_project(str(project_id))
    await service._update_summary_md(project)
    return project


@router.post("/{project_id}/close", response_model=ProjectResponse)
async def close_project(
    project_id: UUID,
    payload: ProjectCloseRequest,
    service: ProjectService = Depends(get_project_service),
) -> ProjectResponse:
    project = await service.close_project(str(project_id), payload)
    await service._update_summary_md(project)
    return project


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


@router.post("/{project_id}/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_project_task(
    project_id: UUID,
    payload: TaskCreate,
    project_service: ProjectService = Depends(get_project_service),
    task_service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    """Create a new task associated with this project.

    The task will be automatically linked to the project and will appear
    in the project's task list. When completed, a journal entry will be
    added to the project.
    """
    # Verify project exists
    await project_service.get_project(str(project_id))

    # Add project to task's project_ids if not already present
    task_data = payload.model_dump()
    project_ids = task_data.get("project_ids", [])
    if str(project_id) not in project_ids:
        project_ids.append(str(project_id))
    task_data["project_ids"] = project_ids

    return await task_service.create_task(TaskCreate.model_validate(task_data))


@router.post("/{project_id}/execute", response_model=ProjectResponse)
async def start_project_execution(
    project_id: UUID,
    execution_service: ProjectExecutionService = Depends(get_project_execution_service),
) -> ProjectResponse:
    """Start auto-execution for a project.
    
    This will:
    1. Transition project to ACTIVE state
    2. Create the first task for step 0
    3. Enable auto-execution mode
    """
    return await execution_service.start_project_execution(str(project_id))


@router.post("/{project_id}/evaluate", response_model=ProjectResponse | None)
async def evaluate_project_completion(
    project_id: UUID,
    execution_service: ProjectExecutionService = Depends(get_project_execution_service),
) -> ProjectResponse | None:
    """Evaluate success criteria and auto-complete project if all criteria met.
    
    Returns the completed project if auto-completed, None otherwise.
    """
    return await execution_service.evaluate_and_complete(str(project_id))
