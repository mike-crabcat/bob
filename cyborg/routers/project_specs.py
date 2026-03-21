"""HTTP routes for project specification versioning."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status

from cyborg.dependencies import get_project_spec_service
from cyborg.models import (
    ProjectSpecApproveRequest,
    ProjectSpecListResponse,
    ProjectSpecRejectRequest,
    ProjectSpecResponse,
    ProjectSpecSubmitRequest,
)
from cyborg.services.project_spec_service import ProjectSpecService


router = APIRouter(prefix="/api/v1", tags=["project-specs"])


@router.post("/projects/{project_id}/specs", response_model=ProjectSpecResponse, status_code=status.HTTP_201_CREATED)
async def submit_project_spec(
    project_id: UUID,
    payload: ProjectSpecSubmitRequest,
    service: ProjectSpecService = Depends(get_project_spec_service),
) -> ProjectSpecResponse:
    """Submit a new project specification for approval."""
    return await service.submit_spec(str(project_id), payload)


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


@router.post("/project-specs/{spec_id}/approve", response_model=ProjectSpecResponse)
async def approve_project_spec(
    spec_id: UUID,
    payload: ProjectSpecApproveRequest,
    service: ProjectSpecService = Depends(get_project_spec_service),
) -> ProjectSpecResponse:
    """Approve a project spec."""
    return await service.approve_spec(str(spec_id), payload)


@router.post("/project-specs/{spec_id}/reject", response_model=ProjectSpecResponse)
async def reject_project_spec(
    spec_id: UUID,
    payload: ProjectSpecRejectRequest,
    service: ProjectSpecService = Depends(get_project_spec_service),
) -> ProjectSpecResponse:
    """Reject a project spec."""
    return await service.reject_spec(str(spec_id), payload)
