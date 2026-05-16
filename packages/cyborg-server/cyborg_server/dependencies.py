"""FastAPI dependency helpers."""

from __future__ import annotations

from fastapi import Depends, Request

from cyborg_server.config import Settings
from cyborg_server.context import AppContext
from cyborg_server.database import Database
from cyborg_server.exceptions import ForbiddenError
from cyborg_server.services.calendar_service import CalendarService
from cyborg_server.services.dispatch_service import DispatchService
from cyborg_server.services.notification_service import NotificationService
from cyborg_server.services.project_execution_service import ProjectExecutionService
from cyborg_server.services.source_discovery_service import SourceDiscoveryService
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


def get_app_context(request: Request) -> AppContext:
    """Build an AppContext from the current request."""

    return AppContext(
        db=request.app.state.db,
        settings=request.app.state.settings,
        event_bus=getattr(request.app.state, "event_bus", None),
    )


def get_task_service(ctx: AppContext = Depends(get_app_context)) -> TaskService:
    """Build a task service for the current request."""

    return TaskService(ctx)


def get_project_service(ctx: AppContext = Depends(get_app_context)) -> ProjectService:
    """Build a project service for the current request."""

    return ProjectService(ctx)


def get_project_spec_service(ctx: AppContext = Depends(get_app_context)) -> ProjectSpecService:
    """Build a project spec service for the current request."""

    return ProjectSpecService(ctx)


def get_calendar_service(ctx: AppContext = Depends(get_app_context)) -> CalendarService:
    """Build a calendar service for the current request."""

    return CalendarService(ctx)


def get_notification_service(ctx: AppContext = Depends(get_app_context)) -> NotificationService:
    """Build a notification service for the current request."""

    return NotificationService(ctx)


def get_session_route_service(ctx: AppContext = Depends(get_app_context)) -> SessionRouteService:
    """Build a session route service for the current request."""

    return SessionRouteService(ctx)


def get_project_execution_service(ctx: AppContext = Depends(get_app_context)) -> ProjectExecutionService:
    """Build a project execution service for the current request."""

    return ProjectExecutionService(ctx)


def get_source_discovery_service(ctx: AppContext = Depends(get_app_context)) -> SourceDiscoveryService:
    """Build a source discovery service for the current request."""

    return SourceDiscoveryService(ctx)


def get_dispatch_service(ctx: AppContext = Depends(get_app_context)) -> DispatchService:
    """Build a dispatch service for the current request."""

    return DispatchService(ctx)
