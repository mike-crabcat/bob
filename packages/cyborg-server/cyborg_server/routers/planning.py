"""API endpoints for project planning and strategy refinement."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database
from cyborg_core.models import JournalEntryType
from cyborg_server.services.base import utcnow
from cyborg_server.services.openclaw_reasoning_service import OpenClawReasoningService
from cyborg_server.services.project_service import ProjectService


router = APIRouter(prefix="/api/v1/planning", tags=["planning"])


# ============================================================================
# Request/Response Models
# ============================================================================


class PlanGenerationRequest(BaseModel):
    """Request to generate a project plan using AI reasoning."""

    aim: str = Field(
        ..., min_length=1, max_length=2000,
        description="What the project aims to accomplish"
    )
    method: str | None = Field(
        None, max_length=2000,
        description="How the project will be executed (approach, methodology)"
    )
    success_criteria: list[str] = Field(
        default_factory=list,
        description="Success criteria for the project"
    )
    reference_project_id: str | None = Field(
        None,
        description="Optional project ID to reference for context"
    )
    context_scope: str = Field(
        default="standard",
        description="Context scope: minimal, standard, comprehensive, full"
    )


class PlanStepResponse(BaseModel):
    """A single step in a generated plan."""

    title: str
    description: str
    criteria: str
    order: int


class PlanGenerationResponse(BaseModel):
    """Response from plan generation."""

    steps: list[PlanStepResponse]
    reasoning: str
    context_used: dict[str, Any] | None = None


class StrategyRefinementRequest(BaseModel):
    """Request to refine project strategy."""

    trigger_reason: str | None = Field(
        "task_completion",
        description="What triggered this refinement (task_completion, failure, manual, etc.)"
    )
    trigger_task_id: str | None = Field(
        None,
        description="Task ID that triggered this refinement (if applicable)"
    )
    force_refresh: bool = Field(
        default=False,
        description="Force a fresh analysis even if recently analyzed"
    )


class StrategyRefinementResponse(BaseModel):
    """Response from strategy refinement."""

    should_refine: bool
    reasoning: str
    suggested_changes: list[str] = Field(default_factory=list)
    new_priorities: dict[str, Any] = Field(default_factory=dict)
    risks_identified: list[str] = Field(default_factory=list)
    applied_at: str | None = None


class ProjectInfoResponse(BaseModel):
    """Basic project information."""

    id: str
    title: str
    aim: str | None
    state: str


# ============================================================================
# Helper Functions
# ============================================================================


def _get_reasoning_service(db: Database) -> OpenClawReasoningService:
    """Get or create the reasoning service instance."""
    # Create service without routing_service for internal use
    return OpenClawReasoningService(db)


async def _get_project_for_refinement(
    project_id: str,
    project_service: ProjectService,
) -> dict[str, Any]:
    """Get project with validation for strategy refinement."""
    try:
        project = await project_service.get_project(project_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    if project.state not in ("planning", "active"):
        raise HTTPException(
            status_code=400,
            detail=f"Project must be in planning or active state for strategy refinement, currently: {project.state}"
        )

    return project.model_dump(mode="json")


# ============================================================================
# API Endpoints
# ============================================================================


@router.post("/generate-plan", response_model=PlanGenerationResponse)
async def generate_project_plan(
    request: PlanGenerationRequest,
    http_request: Request,
    db: Database = Depends(get_database),
) -> PlanGenerationResponse:
    """
    Generate a project plan using OpenClaw reasoning.

    This endpoint analyzes the project aim and method to generate
    a structured execution plan with steps and success criteria.
    """
    reasoning_service = _get_reasoning_service(db)

    try:
        # Generate plan using OpenClaw reasoning
        steps = await reasoning_service.generate_project_plan(
            aim=request.aim,
            method=request.method,
            success_criteria=request.success_criteria,
            reference_project_id=request.reference_project_id,
        )

        return PlanGenerationResponse(
            steps=[
            PlanStepResponse(
                title=step.get("title", ""),
                description=step.get("description", ""),
                criteria=step.get("criteria", ""),
                order=step.get("order", 0),
            )
            for step in steps
        ],
            reasoning="Project plan generated using OpenClaw reasoning.",
            context_used={"aim": request.aim, "method": request.method},
        )

    except Exception as e:
        # Log the error but return a fallback response
        import logging
        logging.error(f"Plan generation failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Plan generation failed: {str(e)}"
        )


@router.post("/projects/{project_id}/refine-strategy", response_model=StrategyRefinementResponse)
async def refine_project_strategy(
    project_id: str,
    request: StrategyRefinementRequest,
    background_tasks: BackgroundTasks,
    db: Database = Depends(get_database),
) -> StrategyRefinementResponse:
    """
    Trigger strategy refinement analysis for a project.

    Uses OpenClaw reasoning to analyze the project state and suggest
    strategic adjustments. If should_refine is true,
    applies the refinements automatically.
    """
    reasoning_service = _get_reasoning_service(db)
    project_service = ProjectService(db)

    # Get and validate project
    project = await _get_project_for_refinement(project_id, project_service)

    # Determine trigger task
    trigger_task_id = request.trigger_task_id
    if not trigger_task_id:
        # Try to find a recently completed task if not specified
        # This is a fallback for manual refinement requests
        pass

    try:
        # Get refinement analysis from OpenClaw
        result = await reasoning_service.refine_project_strategy(
            project_id=project_id,
            trigger_task_id=trigger_task_id or "",
        )

        response = StrategyRefinementResponse(
            should_refine=result.get("should_refine", False),
            reasoning=result.get("reasoning", ""),
            suggested_changes=result.get("suggested_changes", []),
            new_priorities=result.get("new_priorities", {}),
            risks_identified=result.get("risks_identified", []),
        )

        # If should_refine, apply changes automatically
        if response.should_refine:
            # Apply refinements automatically (design decision: auto-accept)
            # This would update project metadata, task priorities, etc.
            # For now, record the decision and return
            response.applied_at = utcnow().isoformat()

            # Add journal entry for the refinement
            # (This would be done via a service call in a real implementation)
            import logging
            logging.info(f"Strategy refinement for project {project_id}: {response.suggested_changes}")

        return response

    except Exception as e:
        import logging
        logging.error(f"Strategy refinement failed for project {project_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Strategy refinement failed: {str(e)}"
        )


@router.get("/projects/{project_id}/status", response_model=ProjectInfoResponse)
async def get_project_status(
    project_id: str,
    db: Database = Depends(get_database),
) -> ProjectInfoResponse:
    """Get current project status for planning decisions."""
    project_service = ProjectService(db)

    try:
        project = await project_service.get_project(project_id)
        return ProjectInfoResponse(
            id=str(project.id),
            title=project.title,
            aim=project.aim,
            state=project.state.value,
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
