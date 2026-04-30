"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cyborg_server import __version__
from cyborg_server.config import Settings
from cyborg_server.database import Database
from cyborg_server.exceptions import ServiceError
from cyborg_server.models import HealthResponse
from cyborg_server.routers import calendars, contacts, context, dashboard, email, health, learning, notifications, openclaw, planning, project_specs, projects, session_routes, tasks, webhooks
from cyborg_server.structured_logging import configure_logging, CorrelationIdMiddleware

logger = logging.getLogger(__name__)

def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""

    resolved_settings = settings or Settings.from_env()

    # Configure structured logging
    configure_logging(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings.ensure_directories()
        database = Database(
            db_path=resolved_settings.db_path,
            schema_dir=Path(__file__).parent / "schemas",
            pool_size=resolved_settings.pool_size,
        )
        await database.connect()
        await database.apply_migrations()
        database.settings = resolved_settings
        app.state.settings = resolved_settings
        app.state.db = database

        # Attach database log handler for structured logging
        from cyborg_server.structured_logging import attach_database_handler
        attach_database_handler(database)

        stop_event = asyncio.Event()
        heartbeat_worker = asyncio.create_task(
            _heartbeat_loop(
                database,
                interval_seconds=resolved_settings.heartbeat_interval_seconds,
                stop_event=stop_event,
            )
        )
        try:
            yield
        finally:
            stop_event.set()
            heartbeat_worker.cancel()
            try:
                await heartbeat_worker
            except asyncio.CancelledError:
                pass
            await database.close()

    # Create FastAPI app
    app = FastAPI(
        title="Cyborg Data Service",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Add correlation ID middleware
    app.add_middleware(CorrelationIdMiddleware)

    @app.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error_handler(_: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health_check(request: Request) -> HealthResponse:
        database: Database = request.app.state.db
        healthy = await database.health_check()
        if not healthy:
            raise RuntimeError("Database health check failed")
        return HealthResponse(status="ok", database="ok")

    app.include_router(tasks.router)
    app.include_router(projects.router)
    app.include_router(project_specs.router)
    app.include_router(calendars.router)
    app.include_router(context.router)
    app.include_router(notifications.router)
    app.include_router(openclaw.router)
    app.include_router(planning.router)
    app.include_router(health.router)
    app.include_router(learning.router)
    app.include_router(session_routes.router)
    app.include_router(webhooks.router, prefix="/api/v1/webhooks")
    app.include_router(contacts.router, prefix="/api/v1")
    app.include_router(email.router)
    app.include_router(dashboard.router)  # Web dashboard

    return app

app = create_app()

async def _heartbeat_loop(database: Database, *, interval_seconds: float, stop_event: asyncio.Event) -> None:
    """Periodically dispatch pending notifications and scan for blocked projects."""

    if interval_seconds <= 0:
        await stop_event.wait()
        return

    from cyborg_server.services.notification_service import NotificationService

    notification_service = NotificationService(database)
    while not stop_event.is_set():
        try:
            await notification_service.dispatch_pending()
        except Exception:
            logger.exception("Heartbeat notification dispatch failed")
        try:
            await _check_blocked_projects(database, notification_service)
        except Exception:
            logger.exception("Heartbeat blocked-project check failed")
        try:
            await _poll_email_inboxes(database)
        except Exception:
            logger.exception("Heartbeat email polling failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _check_blocked_projects(database: Database, notification_service: NotificationService) -> None:
    """Find blocked projects missing notifications and raise one."""
    from cyborg_server.models import ProjectState

    blocked = await database.fetch_all(
        """SELECT id FROM projects
           WHERE deleted_at IS NULL AND state = ? AND blocked_reason IS NOT NULL""",
        (ProjectState.PAUSED.value,),
    )
    for project in blocked:
        await notification_service.sync_project_state(project["id"])


async def _poll_email_inboxes(database: Database) -> None:
    """Poll AgentMail inboxes for new email messages."""
    from cyborg_server.config import Settings

    settings = getattr(database, "settings", None)
    if not isinstance(settings, Settings) or not settings.agentmail.enabled or not settings.email_polling_enabled:
        return

    from cyborg_server.services.agentmail_client import AgentMailClient
    from cyborg_server.services.email_polling_service import EmailPollingService

    client = AgentMailClient(
        base_url=settings.agentmail.base_url,
        api_key=settings.agentmail.api_key,
    )
    try:
        service = EmailPollingService(database, agentmail_client=client)
        count = await service.poll_all_inboxes()
        if count > 0:
            logger.info("Email polling processed %d new message(s)", count)
    finally:
        await client.close()
