"""FastAPI dependency helpers."""

from __future__ import annotations

from fastapi import Depends, Request

from cyborg_core.config import Settings
from cyborg_server.database import Database
from cyborg_core.exceptions import ForbiddenError
from cyborg_server.services.calendar_service import CalendarService
from cyborg_server.services.notification_service import NotificationService
from cyborg_server.services.project_execution_service import ProjectExecutionService
from cyborg_server.services.project_spec_service import ProjectSpecService
from cyborg_server.services.project_service import ProjectService
from cyborg_server.services.session_route_service import SessionRouteService
from cyborg_server.services.task_service import TaskService

def get_settings(request: Request) -> Settings:
    """Return the application settings instance."""

    return request.app.state.settings


def require_dashboard_origin(request: Request) -> None:
    """Verify the request originates from the dashboard.

    If CYBORG_DASHBOARD_SECRET is not configured, the check is skipped (dev mode).
    Otherwise, the request must include the secret as a cookie or header.
    """
    settings: Settings = request.app.state.settings
    if not settings.dashboard_secret_configured:
        return

    secret = request.cookies.get("cyborg_dashboard_secret") or request.headers.get(
        "X-Dashboard-Secret", ""
    )
    if secret != settings.dashboard_secret:
        raise ForbiddenError("This operation requires dashboard authorization")

def get_database(request: Request) -> Database:
    """Return the shared database pool."""

    return request.app.state.db

def get_task_service(database: Database = Depends(get_database)) -> TaskService:
    """Build a task service for the current request."""

    return TaskService(database)

def get_project_service(database: Database = Depends(get_database)) -> ProjectService:
    """Build a project service for the current request."""

    return ProjectService(database)

def get_project_spec_service(database: Database = Depends(get_database)) -> ProjectSpecService:
    """Build a project spec service for the current request."""

    return ProjectSpecService(database)

def get_calendar_service(database: Database = Depends(get_database)) -> CalendarService:
    """Build a calendar service for the current request."""

    return CalendarService(database)

def get_notification_service(database: Database = Depends(get_database)) -> NotificationService:
    """Build a notification service for the current request."""

    return NotificationService(database)

def get_session_route_service(database: Database = Depends(get_database)) -> SessionRouteService:
    """Build a session route service for the current request."""

    return SessionRouteService(database)

def get_project_execution_service(database: Database = Depends(get_database)) -> ProjectExecutionService:
    """Build a project execution service for the current request."""

    return ProjectExecutionService(database)
