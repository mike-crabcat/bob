"""FastAPI dependency helpers."""

from __future__ import annotations

from fastapi import Depends, Request

from bob_server.config import Settings
from bob_server.context import AppContext
from bob_server.database import Database
from bob_server.exceptions import ForbiddenError
from bob_server.services.calendar_service import CalendarService
from bob_server.services.session_route_service import SessionRouteService


def get_settings(request: Request) -> Settings:
    """Return the application settings instance."""

    return request.app.state.settings


def require_dashboard_origin(request: Request) -> None:
    """Verify the request originates from the dashboard.

    If BOB_DASHBOARD_SECRET is not configured, the check is skipped (dev mode).
    Otherwise, the request must include the secret as a cookie or header.
    """
    settings: Settings = request.app.state.settings
    if not settings.dashboard_secret_configured:
        return

    secret = request.cookies.get("bob_dashboard_secret") or request.headers.get(
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


def get_calendar_service(ctx: AppContext = Depends(get_app_context)) -> CalendarService:
    """Build a calendar service for the current request."""

    return CalendarService(ctx)


def get_session_route_service(ctx: AppContext = Depends(get_app_context)) -> SessionRouteService:
    """Build a session route service for the current request."""

    return SessionRouteService(ctx)
