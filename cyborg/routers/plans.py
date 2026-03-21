"""HTTP routes for plan versioning."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status

from cyborg.dependencies import get_plan_service
from cyborg.models import (
    PlanApproveRequest,
    PlanCreate,
    PlanListResponse,
    PlanRejectRequest,
    PlanResponse,
    PlanSubmitRequest,
)
from cyborg.services.plan_service import PlanService


router = APIRouter(prefix="/api/v1", tags=["plans"])


@router.post("/tasks/{task_id}/plans", response_model=PlanResponse, status_code=status.HTTP_201_CREATED)
async def create_plan(
    task_id: UUID,
    payload: PlanSubmitRequest,
    service: PlanService = Depends(get_plan_service),
) -> PlanResponse:
    """Submit a new plan version for a task.
    
    The plan is created with status 'pending_approval'.
    The task must be in 'planning' status to submit a plan.
    """
    return await service.create_plan(str(task_id), payload)


@router.get("/tasks/{task_id}/plans", response_model=PlanListResponse)
async def list_plans(
    task_id: UUID,
    service: PlanService = Depends(get_plan_service),
) -> PlanListResponse:
    """List all plan versions for a task."""
    return await service.list_plans(str(task_id))


@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def get_plan(
    plan_id: UUID,
    service: PlanService = Depends(get_plan_service),
) -> PlanResponse:
    """Get a specific plan by ID."""
    return await service.get_plan(str(plan_id))


@router.post("/plans/{plan_id}/approve", response_model=PlanResponse)
async def approve_plan(
    plan_id: UUID,
    payload: PlanApproveRequest,
    service: PlanService = Depends(get_plan_service),
) -> PlanResponse:
    """Approve a plan.
    
    The plan status changes to 'approved' and the task moves to 'pending'.
    """
    return await service.approve_plan(str(plan_id), payload)


@router.post("/plans/{plan_id}/reject", response_model=PlanResponse)
async def reject_plan(
    plan_id: UUID,
    payload: PlanRejectRequest,
    service: PlanService = Depends(get_plan_service),
) -> PlanResponse:
    """Reject a plan with feedback.
    
    The plan status changes to 'rejected' and the task remains in 'planning' status.
    A new plan must be submitted before the task can become active.
    """
    return await service.reject_plan(str(plan_id), payload)
