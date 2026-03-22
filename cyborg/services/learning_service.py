"""Service for extracting and applying learnings from completed projects."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from cyborg.database import Database
from cyborg.models import ProjectState
from cyborg.services.base import BaseService, utcnow
from cyborg.services.openclaw_reasoning_service import OpenClawReasoningService


class LearningService(BaseService):
    """Extract and manage learnings from completed projects.

    This service:
    - Extracts insights after project completion
    - Stores insights in project_insights table
    - Queries similar projects for context
    - Provides recommendations based on past outcomes
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

    async def extract_insights(
        self,
        project_id: str,
        extracted_by: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Extract and store insights from a completed project.

        Returns list of created insight records.
        """
        # Get project details
        project = await self.db.fetch_one(
            """
            SELECT id, title, aim, method, state, closed_at, conclusion,
                   (SELECT COUNT(*) FROM project_tasks pt WHERE pt.project_id = projects.id) as task_count
            FROM projects
            WHERE id = ? AND deleted_at IS NULL
            """,
            (project_id,),
        )

        if not project:
            return []

        # Only extract from closed/partial projects
        if project["state"] not in (ProjectState.CLOSED.value, "closed"):
            return []

        # Skip very young projects (< 7 days) unless manually requested
        # This is configurable via the extracted_by parameter
        if project["closed_at"] and not extracted_by:
            from datetime import timedelta
            closed = utcnow().fromisoformat(project["closed_at"])
            age = utcnow() - closed
            if age < timedelta(days=7):
                return []

        # Determine outcome type
        task_count = project.get("task_count", 0)
        if project.get("conclusion"):
            # Simple heuristic based on conclusion
            conclusion_lower = project["conclusion"].lower()
            if any(word in conclusion_lower for word in ("success", "achieved", "completed", "done")):
                outcome = "success"
            elif any(word in conclusion_lower for word in ("failed", "abandoned", "cancelled")):
                outcome = "failure"
            else:
                outcome = "partial"
        elif task_count > 0:
            outcome = "success"  # Has completed tasks
        else:
            outcome = "failure"  # No tasks completed

        # Use OpenClaw to extract insights if available
        if outcome in ("success", "partial"):
            try:
                insights = await self.reasoning_service.extract_learnings(project_id)
            except Exception:
                # Fall back to basic extraction
                insights = self._extract_basic_insights(project, outcome)
        else:
            # For failures, extract manually
            insights = self._extract_basic_insights(project, outcome)

        # Store insights in database
        created_insights = []
        for insight in insights:
            insight_id = str(uuid4())
            await self.db.execute(
                """
                INSERT INTO project_insights (
                    id, project_id, outcome_type, insight_category,
                    insight_data, applicability_pattern, extracted_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    insight_id,
                    project_id,
                    outcome,
                    insight.get("category", "execution"),
                    json.dumps(insight),
                    json.dumps(insight.get("applicability", {})),
                    extracted_by or "system",
                    utcnow().isoformat(),
                ),
            )
            created_insights.append(
                {
                    "id": insight_id,
                    "project_id": project_id,
                    **insight,
                }
            )

        return created_insights

    def _extract_basic_insights(self, project: dict[str, Any], outcome: str) -> list[dict[str, Any]]:
        """Fallback basic insight extraction without OpenClaw."""
        insights = []

        # Extract keywords from aim/method for applicability
        aim = project.get("aim", "").lower()
        method = project.get("method", "").lower()
        keywords = list(set(
            [w for w in aim.split() if len(w) > 3]
            + [w for w in method.split() if len(w) > 3]
        ))[:5]

        insights.append({
            "category": "execution",
            "lesson": f"Project completed with outcome: {outcome}",
            "insight": f"Project '{project['title']}' completed with {task_count} tasks.",
            "applicability": {"keywords": keywords, "project_type": project.get("aim", "")[:50]},
        })

        return insights

    async def query_similar_projects(
        self,
        aim: str,
        method: str | None = None,
        limit: int = 5,
        min_outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Query for similar projects based on aim/method and their insights.

        Returns projects with their insights ordered by relevance.
        """
        # Build search terms
        search_terms = [w.lower() for w in aim.split() if len(w) > 3]

        if method:
            search_terms.extend([w.lower() for w in method.split() if len(w) > 3])

        # Get insights from successful projects
        outcome_filter = ""
        if min_outcome:
            outcome_filter = f"AND pi.outcome_type = '{min_outcome}'"
        else:
            outcome_filter = "AND pi.outcome_type IN ('success', 'partial')"

        projects_with_insights = await self.db.fetch_all(
            f"""
            SELECT DISTINCT
                p.id,
                p.title,
                p.aim,
                p.method,
                p.state,
                p.closed_at,
                COUNT(pi.id) as insight_count
            FROM projects p
            INNER JOIN project_insights pi ON pi.project_id = p.id
            WHERE p.deleted_at IS NULL
              AND p.state = 'closed'
              {outcome_filter}
            GROUP BY p.id
            ORDER BY p.closed_at DESC
            LIMIT ?
            """,
            (limit,),
        )

        # Filter by keyword similarity
        similar_projects = []
        for project in projects_with_insights:
            project_aim = (project.get("aim") or "").lower()
            project_method = (project.get("method") or "").lower()
            combined_text = f"{project_aim} {project_method}"

            match_count = sum(1 for term in search_terms if term in combined_text)
            if match_count >= 1:
                # Get the insights for this project
                insights = await self.db.fetch_all(
                    """
                    SELECT id, insight_category, insight_data, applicability_pattern
                    FROM project_insights
                    WHERE project_id = ?
                    ORDER BY created_at DESC
                    """,
                    (project["id"],),
                )

                similar_projects.append({
                    "id": project["id"],
                    "title": project["title"],
                    "aim": project.get("aim"),
                    "method": project.get("method"),
                    "state": project["state"],
                    "closed_at": project.get("closed_at"),
                    "insight_count": project["insight_count"],
                    "match_score": match_count,
                    "insights": [
                        {
                            "id": insight["id"],
                            "category": insight["insight_category"],
                            "data": json.loads(insight["insight_data"]),
                            "applicability": json.loads(insight.get("applicability_pattern") or "{}"),
                        }
                        for insight in insights
                    ],
                })

        # Sort by match score and return
        similar_projects.sort(key=lambda p: p["match_score"], reverse=True)
        return similar_projects[:limit]

    async def get_active_insights(
        self,
        category: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Get active (successful/partial) insights that can be applied to new projects.

        Returns insights ordered by recency.
        """
        category_filter = ""
        params = [limit]
        if category:
            category_filter = "AND insight_category = ?"
            params.insert(0, category)

        insights = await self.db.fetch_all(
            f"""
            SELECT
                id, project_id, outcome_type, insight_category,
                insight_data, applicability_pattern, created_at, extracted_by
            FROM active_insights
            WHERE 1=1
            {category_filter}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        )

        return [
            {
                "id": insight["id"],
                "project_id": insight["project_id"],
                "outcome": insight["outcome_type"],
                "category": insight["insight_category"],
                "data": json.loads(insight["insight_data"]),
                "applicability": json.loads(insight.get("applicability_pattern") or "{}"),
                "created_at": insight["created_at"],
                "extracted_by": insight.get("extracted_by"),
            }
            for insight in insights
        ]

    async def suggest_success_criteria(
        self,
        aim: str,
        method: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Suggest success criteria based on similar past projects.
        """
        similar_projects = await self.query_similar_projects(
            aim=aim,
            method=method,
            limit=5,
            min_outcome="success",
        )

        # Extract criteria patterns from similar projects
        criteria: list[dict[str, Any]] = []

        for project in similar_projects:
            # Get project's success criteria
            project_criteria = await self.db.fetch_one(
                """
                SELECT success_criteria
                FROM project_specs
                WHERE project_id = ? AND is_current = 1
                """,
                (project["id"],),
            )

            if project_criteria:
                try:
                    crit_list = json.loads(project_criteria.get("success_criteria") or "[]")
                    for crit in crit_list:
                        if crit not in criteria:  # Avoid duplicates
                            criteria.append({
                                "check": crit.get("check", ""),
                                "description": crit.get("description", ""),
                                "source_project": project["title"],
                                "source_project_id": project["id"],
                            })
                except json.JSONDecodeError:
                    pass

        return criteria[:5]  # Return top 5 suggested criteria
