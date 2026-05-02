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
from cyborg_server.context import AppContext
from cyborg_server.database import Database
from cyborg_server.exceptions import ServiceError
from cyborg_server.heartbeat import (
    BlockedProjectCheckTask,
    EmailPollingTask,
    EmailSyncTask,
    HeartbeatRunner,
    NotificationDispatchTask,
    StuckDispatchCheckTask,
)
from cyborg_server.models import HealthResponse
from cyborg_server.routers import calendars, contacts, context, dashboard, dispatches, email, health, learning, notifications, openclaw, planning, project_specs, projects, session_routes, tasks, webhooks
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

        app_ctx = AppContext(db=database, settings=resolved_settings)

        # Attach database log handler for structured logging
        from cyborg_server.structured_logging import attach_database_handler
        attach_database_handler(database)

        stop_event = asyncio.Event()
        runner = HeartbeatRunner(app_ctx, interval_seconds=resolved_settings.heartbeat_interval_seconds)
        runner.register(NotificationDispatchTask())
        runner.register(BlockedProjectCheckTask())
        runner.register(EmailPollingTask())
        runner.register(StuckDispatchCheckTask())
        runner.register(EmailSyncTask())
        heartbeat_worker = asyncio.create_task(runner.run_loop(stop_event))
        try:
            yield
        finally:
            stop_event.set()

            # Wait for active dispatches to complete before shutting down
            try:
                from cyborg_server.services.dispatch_service import DispatchService
                dispatch_service = DispatchService(app_ctx)
                active = await dispatch_service.count_active_dispatches()
                if active:
                    print(f"Waiting for {active} active dispatch(es) to complete...")
                    await dispatch_service.wait_for_active_dispatches(
                        timeout_seconds=resolved_settings.dispatch_shutdown_timeout_seconds,
                        poll_interval=2.0,
                    )
                    remaining = await dispatch_service.count_active_dispatches()
                    if remaining:
                        print(f"Shutdown timed out — cancelled {remaining} dispatch(es).")
                    else:
                        print("All dispatches completed. Shutting down.")
            except Exception:
                logger.warning("Error during dispatch drain", exc_info=True)

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
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

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
    app.include_router(dispatches.router)

    return app

app = create_app()
