"""FastAPI dependency helpers."""

from __future__ import annotations

from fastapi import Depends, Request

from cyborg.config import Settings
from cyborg.database import Database
from cyborg.services.calendar_service import CalendarService
from cyborg.services.project_service import ProjectService
from cyborg.services.task_service import TaskService


def get_settings(request: Request) -> Settings:
    """Return the application settings instance."""

    return request.app.state.settings


def get_database(request: Request) -> Database:
    """Return the shared database pool."""

    return request.app.state.db


def get_task_service(database: Database = Depends(get_database)) -> TaskService:
    """Build a task service for the current request."""

    return TaskService(database)


def get_project_service(database: Database = Depends(get_database)) -> ProjectService:
    """Build a project service for the current request."""

    return ProjectService(database)


def get_calendar_service(database: Database = Depends(get_database)) -> CalendarService:
    """Build a calendar service for the current request."""

    return CalendarService(database)
