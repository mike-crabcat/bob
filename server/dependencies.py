"""FastAPI dependency helpers."""

from __future__ import annotations

from fastapi import Depends, Request

from server.config import Settings
from server.context import AppContext
from server.database import Database
from server.exceptions import ForbiddenError
from server.services.calendar_service import CalendarService


def get_settings(request: Request) -> Settings:
    """Return the application settings instance."""

    return request.app.state.settings


def require_dashboard_origin(request: Request) -> None:
    """Verify the request carries the API token.

    Currently unused by any router (kept for reuse); delegates to the shared
    comparison path in api_auth so it cannot drift from the middleware.
    """
    from server.api_auth import api_token_valid

    if not api_token_valid(request.app.state.settings, request):
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
