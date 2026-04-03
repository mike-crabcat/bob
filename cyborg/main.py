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

from cyborg import __version__
from cyborg.config import Settings
from cyborg.database import Database
from cyborg.exceptions import ServiceError
from cyborg.models import HealthResponse
from cyborg.routers import calendars, contacts, context, dashboard, health, learning, notifications, openclaw, planning, project_specs, projects, session_routes, tasks, webhooks
from cyborg.structured_logging import configure_logging, CorrelationIdMiddleware

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
        from cyborg.structured_logging import attach_database_handler
        attach_database_handler(database)

        stop_event = asyncio.Event()
        notification_worker = asyncio.create_task(
            _notification_loop(
                database,
                interval_seconds=resolved_settings.notification_dispatch_interval_seconds,
                stop_event=stop_event,
            )
        )
        try:
            yield
        finally:
            stop_event.set()
            notification_worker.cancel()
            try:
                await notification_worker
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
    app.include_router(dashboard.router)  # Web dashboard

    return app

app = create_app()

async def _notification_loop(database: Database, *, interval_seconds: float, stop_event: asyncio.Event) -> None:
    """Periodically sync and dispatch notifications without relying on client polling."""

    if interval_seconds <= 0:
        await stop_event.wait()
        return

    from cyborg.services.notification_service import NotificationService

    service = NotificationService(database)
    while not stop_event.is_set():
        try:
            await service.process_due_notifications()
        except Exception:
            # Notification processing is best-effort and must not take down the API.
            logger.exception("Notification processing failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
