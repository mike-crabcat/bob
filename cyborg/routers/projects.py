"""HTTP routes for project management."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from cyborg.dependencies import get_project_service
from cyborg.models import (
    ProjectCloseRequest,
    ProjectCreate,
    ProjectJournalEntryCreate,
    ProjectJournalEntryResponse,
    ProjectResponse,
    ProjectState,
    ProjectUpdate,
    TaskResponse,
)
from cyborg.services.project_service import ProjectService


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
    return await service.start_project(str(project_id))


@router.post("/{project_id}/pause", response_model=ProjectResponse)
async def pause_project(project_id: UUID, service: ProjectService = Depends(get_project_service)) -> ProjectResponse:
    return await service.pause_project(str(project_id))


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
