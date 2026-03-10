"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
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
from cyborg.routers import calendars, context, projects, tasks


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""

    resolved_settings = settings or Settings.from_env()

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
        app.state.settings = resolved_settings
        app.state.db = database
        try:
            yield
        finally:
            await database.close()

    app = FastAPI(
        title="Cyborg Data Service",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

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
    app.include_router(calendars.router)
    app.include_router(context.router)

    return app


app = create_app()
