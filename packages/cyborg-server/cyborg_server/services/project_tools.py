"""Pre-built LLM tools for project and task management.

Usage:
    tools = make_project_tools(ctx)
    result = await dispatch.chat_with_tools(messages, tools, call_category="notification")
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from cyborg_server.context import AppContext
from cyborg_server.services.tools import tool

logger = logging.getLogger(__name__)


def make_project_tools(ctx: AppContext):
    """Create a list of project management tools bound to the given context."""

    @tool
    async def create_task(
        title: str,
        project_id: str,
        description: str = "",
        plan: str = "",
        priority: Literal["low", "medium", "high", "critical"] = "medium",
    ) -> str:
        """Create a new task in a project. Returns the task ID and title."""
        from cyborg_server.services.task_service import TaskService

        svc = TaskService(ctx)
        result = await svc.create_task({
            "title": title,
            "description": description,
            "plan": plan,
            "priority": priority,
            "project_ids": [project_id],
        })
        return json.dumps({"task_id": result.id, "title": result.title, "status": result.status})

    @tool
    async def close_project(
        project_id: str,
        conclusion: str = "",
    ) -> str:
        """Close a project as completed with a conclusion."""
        from cyborg_server.services.project_service import ProjectService
        from cyborg_server.models import ProjectCloseRequest

        svc = ProjectService(ctx)
        result = await svc.close_project(project_id, ProjectCloseRequest(conclusion=conclusion))
        return json.dumps({"project_id": result.id, "state": result.state})

    @tool
    async def block_project(
        project_id: str,
        reason: str,
        resume_instructions: str = "",
    ) -> str:
        """Block a project waiting for human input. Provide reason and how to resume."""
        from cyborg_server.services.project_execution_service import ProjectExecutionService

        svc = ProjectExecutionService(ctx)
        result = await svc.block_project(project_id, reason, resume_instructions or None)
        return json.dumps({"project_id": result.id, "state": result.state, "reason": reason})

    @tool
    async def list_project_tasks(
        project_id: str,
        status: str = "",
    ) -> str:
        """List tasks in a project. Optionally filter by status (pending, active, completed, failed, blocked)."""
        from cyborg_server.services.task_service import TaskService

        svc = TaskService(ctx)
        tasks = await svc.list_tasks()
        # Filter to project tasks
        project_tasks = []
        for t in tasks:
            # Check if task belongs to project via project_tasks table
            links = await ctx.db.fetch_all(
                "SELECT project_id FROM project_tasks WHERE task_id = ?", (t.id,)
            )
            if any(link["project_id"] == project_id for link in links):
                if not status or t.status == status:
                    project_tasks.append({
                        "id": t.id,
                        "title": t.title,
                        "status": t.status,
                        "priority": t.priority,
                    })
        return json.dumps(project_tasks)

    return [create_task, close_project, block_project, list_project_tasks]
