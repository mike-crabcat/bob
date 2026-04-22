"""API endpoints for project insights and learning."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database
from cyborg_server.services.learning_service import LearningService


router = APIRouter(prefix="/api/v1/learning", tags=["learning"])


# ============================================================================
# Request/Response Models
# ============================================================================


class ExtractInsightsRequest(BaseModel):
    """Request to extract insights from a completed project."""

    force: bool = Field(
        default=False,
        description="Extract insights even if project is very young"
    )


class ExtractInsightsResponse(BaseModel):
    """Response from insight extraction."""

    project_id: str
    insights_extracted: int
    insights: list[dict[str, Any]] = Field(default_factory=list)


class SimilarProjectsRequest(BaseModel):
    """Request to find similar projects."""

    aim: str = Field(..., min_length=1, description="Project aim to match against")
    method: str | None = Field(None, description="Project method to match")
    limit: int = Field(default=5, ge=1, le=20, description="Max projects to return")
    min_outcome: str | None = Field(
        None,
        description="Filter by outcome: success, failure, or partial"
    )


class SimilarProjectsResponse(BaseModel):
    """Response with similar projects and their insights."""

    projects: list[dict[str, Any]] = Field(default_factory=list)
    total_found: int


class ActiveInsightsResponse(BaseModel):
    """Response with active insights from successful projects."""

    insights: list[dict[str, Any]] = Field(default_factory=list)
    total: int


class SuggestCriteriaResponse(BaseModel):
    """Response with suggested success criteria."""

    criteria: list[dict[str, Any]] = Field(default_factory=list)


# ============================================================================
# Helper Functions
# ============================================================================


def _get_learning_service(db: Database) -> LearningService:
    """Get or create the learning service instance."""
    return LearningService(db)


# ============================================================================
# API Endpoints
# ============================================================================


@router.post("/projects/{project_id}/extract-insights", response_model=ExtractInsightsResponse)
async def extract_project_insights(
    project_id: str,
    request: ExtractInsightsRequest | None = None,
    db: Database = Depends(get_database),
) -> ExtractInsightsResponse:
    """
    Extract and store insights from a completed project.

    Uses OpenClaw reasoning to analyze the project and identify learnings.
    """
    learning_service = _get_learning_service(db)

    # Verify project exists
    project = await db.fetch_one(
        "SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL",
        (project_id,),
    )
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    # Extract insights
    extracted_by = "manual" if request and request.force else "system"
    insights = await learning_service.extract_insights(project_id, extracted_by=extracted_by)

    return ExtractInsightsResponse(
        project_id=project_id,
        insights_extracted=len(insights),
        insights=insights,
    )


@router.get("/similar-projects", response_model=SimilarProjectsResponse)
async def find_similar_projects(
    aim: str,
    method: str | None = None,
    limit: int = 5,
    min_outcome: str | None = None,
    db: Database = Depends(get_database),
) -> SimilarProjectsResponse:
    """
    Find projects similar to the given aim/method.

    Returns projects with their insights, ordered by relevance.
    """
    learning_service = _get_learning_service(db)

    projects = await learning_service.query_similar_projects(
        aim=aim,
        method=method,
        limit=limit,
        min_outcome=min_outcome,
    )

    return SimilarProjectsResponse(
        projects=projects,
        total_found=len(projects),
    )


@router.get("/insights/active", response_model=ActiveInsightsResponse)
async def get_active_insights(
    category: str | None = None,
    limit: int = 50,
    db: Database = Depends(get_database),
) -> ActiveInsightsResponse:
    """
    Get active (successful/partial) insights that can be applied to new projects.

    Insights are extracted from completed successful projects.
    """
    learning_service = _get_learning_service(db)

    insights = await learning_service.get_active_insights(
        category=category,
        limit=limit,
    )

    return ActiveInsightsResponse(
        insights=insights,
        total=len(insights),
    )


@router.post("/suggest-criteria", response_model=SuggestCriteriaResponse)
async def suggest_success_criteria(
    aim: str,
    method: str | None = None,
    db: Database = Depends(get_database),
) -> SuggestCriteriaResponse:
    """
    Suggest success criteria based on similar successful projects.

    Returns criteria from similar past projects.
    """
    learning_service = _get_learning_service(db)

    criteria = await learning_service.suggest_success_criteria(
        aim=aim,
        method=method,
    )

    return SuggestCriteriaResponse(
        criteria=criteria,
    )
