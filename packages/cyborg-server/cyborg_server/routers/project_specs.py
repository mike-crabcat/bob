"""HTTP routes for project specification versioning."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, status

from cyborg_server.dependencies import get_project_spec_service, require_dashboard_origin
from cyborg_core.models import (
    ProjectSpecListResponse,
    ProjectSpecResponse,
    ProjectSpecSubmitRequest,
)
from cyborg_server.services.project_spec_service import ProjectSpecService


router = APIRouter(prefix="/api/v1", tags=["project-specs"])


@router.post("/projects/{project_id}/specs", response_model=ProjectSpecResponse, status_code=status.HTTP_201_CREATED)
async def submit_project_spec(
    project_id: UUID,
    payload: ProjectSpecSubmitRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_dashboard_origin),
    service: ProjectSpecService = Depends(get_project_spec_service),
) -> ProjectSpecResponse:
    """Submit a new project specification for approval."""
    spec = await service.submit_spec(str(project_id), payload, defer_plan_generation=True)
    if not payload.plan:
        background_tasks.add_task(service.generate_plan_if_needed, str(project_id))
    return spec


@router.get("/projects/{project_id}/specs", response_model=ProjectSpecListResponse)
async def list_project_specs(
    project_id: UUID,
    service: ProjectSpecService = Depends(get_project_spec_service),
) -> ProjectSpecListResponse:
    """List all specs for a project."""
    return await service.list_specs(str(project_id))


@router.get("/project-specs/{spec_id}", response_model=ProjectSpecResponse)
async def get_project_spec(
    spec_id: UUID,
    service: ProjectSpecService = Depends(get_project_spec_service),
) -> ProjectSpecResponse:
    """Get a specific project spec by ID."""
    return await service.get_spec(str(spec_id))
