"""API endpoints for project health monitoring."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from cyborg_server.context import AppContext
from cyborg_server.dependencies import get_app_context
from cyborg_server.models import ProjectState
from cyborg_server.services.base import utcnow
from cyborg_server.services.health_monitor_service import HealthMonitorService


router = APIRouter(prefix="/api/v1/health", tags=["health"])


# ============================================================================
# Request/Response Models
# ============================================================================


class ProjectHealthResponse(BaseModel):
    """Health assessment for a project."""

    project_id: str
    project_title: str | None = None
    health_score: float = Field(..., ge=0, le=1, description="0-1 health score")
    risk_level: str = Field(..., description="low, medium, high, or critical")
    indicators: dict[str, Any] = Field(default_factory=dict)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    analysis_timestamp: str | None = None


class HealthScanResponse(BaseModel):
    """Response from health scan across multiple projects."""

    scanned_count: int
    total_projects: int
    projects: list[ProjectHealthResponse]
    timestamp: str


class ProjectsNeedingAttentionResponse(BaseModel):
    """Projects that require intervention."""

    project_id: str
    title: str
    state: str
    health_score: float
    risk_level: str
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    last_check_at: str | None = None


# ============================================================================
# Helper Functions
# ============================================================================


def _get_health_service(ctx: AppContext) -> HealthMonitorService:
    """Get or create the health monitor service instance."""
    return HealthMonitorService(ctx)


# ============================================================================
# API Endpoints
# ============================================================================


@router.get("/scan", response_model=HealthScanResponse)
async def scan_project_health(
    include_healthy: bool = False,
    ctx: AppContext = Depends(get_app_context),
) -> HealthScanResponse:
    """
    Scan all active projects for health issues.

    Returns projects with health assessments, sorted by risk level.
    """
    health_service = _get_health_service(ctx)
    db = ctx.db

    # Get total active project count
    total_projects = await db.fetch_one(
        """SELECT COUNT(*) as count FROM projects WHERE state = ? AND deleted_at IS NULL""",
        (ProjectState.ACTIVE.value,),
    )
    total = int(total_projects["count"]) if total_projects else 0

    # Scan all projects
    results = await health_service.scan_all_projects(include_healthy=include_healthy)

    return HealthScanResponse(
        scanned_count=len(results),
        total_projects=total,
        projects=[
            ProjectHealthResponse(**r)
            for r in results
        ],
        timestamp=utcnow().isoformat(),
    )


@router.get("/projects-needing-attention", response_model=list[ProjectsNeedingAttentionResponse])
async def get_projects_needing_attention(
    limit: int = 20,
    ctx: AppContext = Depends(get_app_context),
) -> list[ProjectsNeedingAttentionResponse]:
    """
    Get projects that need attention (high/critical risk or alerts).

    Returns projects sorted by urgency.
    """
    health_service = _get_health_service(ctx)

    results = await health_service.get_projects_needing_attention(limit=limit)

    return [ProjectsNeedingAttentionResponse(**r) for r in results]


@router.get("/projects/{project_id}/health", response_model=ProjectHealthResponse)
async def get_project_health(
    project_id: str,
    save_check: bool = False,
    ctx: AppContext = Depends(get_app_context),
) -> ProjectHealthResponse:
    """
    Get health analysis for a specific project.

    If save_check is True, saves the health check to the database.
    """
    health_service = _get_health_service(ctx)
    db = ctx.db

    # Verify project exists
    project = await db.fetch_one(
        "SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL",
        (project_id,),
    )
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    # Analyze health
    health = await health_service.analyze_project_health(project_id)

    # Save check if requested
    if save_check:
        await health_service.save_health_check(
            project_id=project_id,
            health_score=health["health_score"],
            risk_level=health["risk_level"],
            indicators=health["indicators"],
            recommendations=health["recommendations"],
            check_type="manual",
        )

    return ProjectHealthResponse(
        project_id=project_id,
        project_title=project.get("title"),
        health_score=health["health_score"],
        risk_level=health["risk_level"],
        indicators=health["indicators"],
        recommendations=health["recommendations"],
        analysis_timestamp=health.get("analysis_timestamp"),
    )


@router.get("/projects/{project_id}/health/latest")
async def get_latest_health_check(
    project_id: str,
    ctx: AppContext = Depends(get_app_context),
) -> dict[str, Any] | None:
    """
    Get the most recent health check for a project.

    Returns the latest saved health check or null if none exist.
    """
    health_service = _get_health_service(ctx)

    result = await health_service.get_latest_health_check(project_id)

    if not result:
        return {"detail": "No health checks found for this project"}

    return result
