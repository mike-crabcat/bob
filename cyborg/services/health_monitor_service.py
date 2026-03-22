"""Service for monitoring project health and identifying at-risk projects."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

from cyborg.database import Database
from cyborg.models import ProjectState, TaskStatus
from cyborg.services.base import BaseService, utcnow
from cyborg.services.openclaw_reasoning_service import OpenClawReasoningService


class HealthMonitorService(BaseService):
    """Monitor project health and identify at-risk projects.

    This service:
    - Analyzes project task states and progress
    - Computes health scores and risk levels
    - Generates alerts for projects needing attention
    - Stores health checks in project_health_checks table
    """

    def __init__(self, db: Database, reasoning_service: OpenClawReasoningService | None = None) -> None:
        super().__init__(db)
        self._reasoning_service = reasoning_service

    @property
    def reasoning_service(self) -> OpenClawReasoningService:
        if self._reasoning_service is None:
            from cyborg.services.openclaw_reasoning_service import OpenClawReasoningService
            self._reasoning_service = OpenClawReasoningService(self.db)
        return self._reasoning_service

    async def analyze_project_health(
        self,
        project_id: str,
        check_type: str = "triggered",
    ) -> dict[str, Any]:
        """
        Analyze the health of a project and return health assessment.

        Returns dict with health_score, risk_level, indicators, and recommendations.
        """
        # Get project stats
        project = await self.db.fetch_one(
            """
            SELECT id, title, aim, method, state, auto_execute, created_at,
                   (SELECT COUNT(*) FROM project_tasks pt INNER JOIN tasks t ON pt.task_id = t.id
                    WHERE pt.project_id = projects.id AND t.deleted_at IS NULL) as task_count
            FROM projects
            WHERE id = ? AND deleted_at IS NULL
            """,
            (project_id,),
        )

        if not project:
            return {
                "health_score": 0,
                "risk_level": "critical",
                "indicators": {},
                "recommendations": ["Project not found"],
            }

        # Get detailed task stats
        task_stats = await self.db.fetch_one(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as blocked,
                SUM(CASE WHEN status IN (?, ?) THEN 1 ELSE 0 END) as active,
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as pending
            FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.deleted_at IS NULL
            """,
            (
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.BLOCKED.value,
                TaskStatus.ACTIVE.value,
                TaskStatus.PENDING.value,
                TaskStatus.PLANNING.value,
                project_id,
            ),
        )

        total = int(task_stats["total"]) if task_stats and task_stats["total"] else 0
        completed = int(task_stats["completed"]) if task_stats and task_stats["completed"] else 0
        failed = int(task_stats["failed"]) if task_stats and task_stats["failed"] else 0
        blocked = int(task_stats["blocked"]) if task_stats and task_stats["blocked"] else 0
        active = int(task_stats["active"]) if task_stats and task_stats["active"] else 0
        pending = int(task_stats["pending"]) if task_stats and task_stats["pending"] else 0

        # Check for blockers specifically
        blockers = await self.db.fetch_all(
            """
            SELECT t.id, t.title, t.blocked_reason, t.blocked_resume_instructions
            FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.status = ? AND t.deleted_at IS NULL
            """,
            (project_id, TaskStatus.BLOCKED.value),
        )

        # Calculate health score
        # Start at 1.0, deduct for issues
        health_score = 1.0

        if total > 0:
            # Deduct for failed tasks (heavier penalty)
            health_score -= (failed * 0.3)
            # Deduct for blocked tasks
            health_score -= (blocked * 0.2)
            # Add points for completed tasks
            health_score += (completed / total) * 0.1

        # Clamp between 0 and 1
        health_score = max(0.0, min(1.0, health_score))

        # Determine risk level
        if health_score >= 0.8:
            risk_level = "low"
        elif health_score >= 0.5:
            risk_level = "medium"
        elif health_score >= 0.3:
            risk_level = "high"
        else:
            risk_level = "critical"

        # Build indicators
        indicators = {
            "total_tasks": total,
            "completed_tasks": completed,
            "failed_tasks": failed,
            "blocked_tasks": blocked,
            "active_tasks": active,
            "pending_tasks": pending,
            "completion_rate": completed / total if total > 0 else 0,
            "blocker_details": [
                {
                    "task_id": b["id"],
                    "task_title": b["title"],
                    "reason": b.get("blocked_reason"),
                }
                for b in blockers
            ],
        }

        # Generate recommendations
        recommendations = []

        if blocked > 0:
            recommendations.append({
                "priority": "high",
                "action": "Address blocked tasks",
                "reason": f"{blocked} task(s) are blocked and preventing progress",
            })

        if failed > 0:
            recommendations.append({
                "priority": "medium",
                "action": "Review failed tasks",
                "reason": f"{failed} task(s) have failed and may need alternative approaches",
            })

        if active > 5:
            recommendations.append({
                "priority": "low",
                "action": "Consider focusing on fewer concurrent tasks",
                "reason": f"{active} active tasks may indicate resource scattering",
            })

        if total > 0 and completed / total < 0.3:
            recommendations.append({
                "priority": "medium",
                "action": "Accelerate task completion",
                "reason": f"Only {completed}/{total} tasks completed, project may be stalled",
            })

        # Use OpenClaw for deeper analysis if available and risk is medium or higher
        if risk_level in ("medium", "high", "critical") and recommendations:
            try:
                ai_analysis = await self.reasoning_service.analyze_project_health(project_id)
                # Merge AI recommendations
                ai_recs = ai_analysis.get("recommendations", [])
                if ai_recs:
                    recommendations.extend(ai_recs)
            except Exception:
                # Fall back to rule-based recommendations
                pass

        return {
            "health_score": round(health_score, 2),
            "risk_level": risk_level,
            "indicators": indicators,
            "recommendations": recommendations,
            "analysis_timestamp": utcnow().isoformat(),
        }

    async def save_health_check(
        self,
        project_id: str,
        health_score: float,
        risk_level: str,
        indicators: dict[str, Any] | None = None,
        recommendations: list[dict[str, Any]] | None = None,
        check_type: str = "triggered",
    ) -> dict[str, Any]:
        """Save a health check record to the database."""
        check_id = str(uuid4())

        await self.db.execute(
            """
            INSERT INTO project_health_checks (
                id, project_id, check_type, health_score, risk_level,
                indicators, recommendations, alert_triggered, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                check_id,
                project_id,
                check_type,
                health_score,
                risk_level,
                json.dumps(indicators or {}),
                json.dumps(recommendations or []),
                risk_level in ("high", "critical"),
                utcnow().isoformat(),
            ),
        )

        return {
            "id": check_id,
            "project_id": project_id,
            "check_type": check_type,
            "health_score": health_score,
            "risk_level": risk_level,
        }

    async def scan_all_projects(
        self,
        include_healthy: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Scan all active projects and return health assessments.

        Returns list of project health results sorted by risk level.
        """
        # Get all active projects
        active_projects = await self.db.fetch_all(
            """
            SELECT id, title, aim, state, auto_execute, created_at
            FROM projects
            WHERE state = ? AND deleted_at IS NULL
            ORDER BY created_at DESC
            """,
            (ProjectState.ACTIVE.value,),
        )

        results = []
        for project in active_projects:
            health = await self.analyze_project_health(project["id"])

            # Skip healthy projects if requested
            if not include_healthy and health["risk_level"] == "low":
                continue

            # Save health check if risk is not low
            if health["risk_level"] != "low":
                await self.save_health_check(
                    project_id=project["id"],
                    health_score=health["health_score"],
                    risk_level=health["risk_level"],
                    indicators=health["indicators"],
                    recommendations=health["recommendations"],
                    check_type="scan",
                )

            results.append({
                "project_id": project["id"],
                "project_title": project["title"],
                **health,
            })

        # Sort by risk level (critical first) then by health score
        risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        results.sort(key=lambda r: (risk_order.get(r["risk_level"], 2), -r["health_score"]))

        return results

    async def get_projects_needing_attention(
        self,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Get projects that need attention based on latest health checks.

        Returns projects with alert_triggered or high/critical risk level.
        """
        # Query the projects_need_attention view
        projects = await self.db.fetch_all(
            f"""
            SELECT * FROM projects_need_attention
            ORDER BY needs_attention DESC, risk_level DESC
            LIMIT ?
            """,
            (limit,),
        )

        return [
            {
                "project_id": p["project_id"],
                "title": p["title"],
                "state": p["state"],
                "check_type": p["check_type"],
                "health_score": p["health_score"],
                "risk_level": p["risk_level"],
                "alert_triggered": p["alert_triggered"],
                "recommendations": json.loads(p.get("recommendations") or "[]"),
                "last_check_at": p.get("last_check_at"),
            }
            for p in projects
        ]

    async def get_latest_health_check(
        self,
        project_id: str,
    ) -> dict[str, Any] | None:
        """Get the most recent health check for a project."""
        result = await self.db.fetch_one(
            """
            SELECT * FROM latest_project_health
            WHERE project_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project_id,),
        )

        if result:
            return {
                "project_id": result["project_id"],
                "check_type": result["check_type"],
                "health_score": result["health_score"],
                "risk_level": result["risk_level"],
                "indicators": json.loads(result.get("indicators") or "{}"),
                "alert_triggered": result["alert_triggered"],
                "recommendations": json.loads(result.get("recommendations") or "[]"),
                "created_at": result["created_at"],
            }
        return None
