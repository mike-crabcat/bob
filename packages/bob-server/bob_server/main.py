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

from bob_server import __version__
from bob_server.api_auth import ApiAuthMiddleware
from bob_server.config import Settings
from bob_server.context import AppContext
from bob_server.database import Database
from bob_server.exceptions import ServiceError
from bob_server.heartbeat import (
    CallCleanupTask,
    DreamTask,
    EmailPollingTask,
    EmailSyncTask,
    HeartbeatRunner,
    LLMCallStalenessTask,
    LlmLogRetentionTask,
    EventLogReconciliationTask,
    AttentionShadowAgreementTask,
    EffectPumpTask,
    ClaimRouterSweepTask,
    OutreachDetectorSweepTask,
    GoalReviewTask,
    WakeupPumpTask,
    DeletionPropagationTask,
    GrowthMonitoringTask,
    LocationFetchTask,
    MemoryReconciliationTask,
    SessionIdleSummaryTask,
)
from bob_server.models import HealthResponse
from bob_server.routers import (
    calendars, contacts, context, dashboard_api, dashboard_ws, email,
    persona, published_files, webhooks, whatsapp,
)
from bob_server.services.event_bus import EventBus
from bob_server.structured_logging import configure_logging, CorrelationIdMiddleware

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

        # Boot sweep: any 'running' llm_call_log row is a zombie from the
        # previous process — nothing survives a restart. Fail them now so
        # the dashboard never shows dead calls as live.
        try:
            from bob_server.repositories.llm_call_log import LlmCallLogRepository
            swept = await LlmCallLogRepository(database).cancel_running()
            if swept:
                logger.info("Boot sweep cancelled %d zombie LLM call(s)", swept)
        except Exception:
            logger.warning("llm_call_log boot sweep failed", exc_info=True)

        # Restart mid-turn recovery: turns still pending/running died with
        # the process, and the messages they claimed were consumed but never
        # answered — the crash-recovery sweep only re-arms *undispatched*
        # messages, so without this a deploy restart mid-LLM silently eats
        # them (2026-08-30: a restart ate an in-flight Dylan question; it
        # sat unanswered until an unrelated nudge 10 minutes later). Restore
        # the claims, then fail the turn; the WhatsApp +10s sweep re-arms.
        try:
            from bob_server.repositories.history import HistoryRepository
            from bob_server.repositories.turns import TurnRepository
            turn_repo = TurnRepository(database)
            zombie_ids = await turn_repo.nonterminal_ids()
            restored = 0
            for tid in zombie_ids:
                restored += await HistoryRepository(database).restore_messages_for_turn(tid)
                await turn_repo.fail(tid, "process restart")
            if zombie_ids:
                logger.info("Boot sweep recovered %d zombie turn(s), restored "
                            "%d claimed message(s) for re-dispatch",
                            len(zombie_ids), restored)
        except Exception:
            logger.warning("zombie-turn boot sweep failed", exc_info=True)

        database.settings = resolved_settings
        app.state.settings = resolved_settings
        app.state.db = database

        app_ctx = AppContext(db=database, settings=resolved_settings)

        # Clean up stale subagents from previous runs
        try:
            from bob_server.services.subagent_service import SubagentService
            await SubagentService(app_ctx).cleanup_stale()
        except Exception:
            logger.debug("Subagent cleanup skipped (table may not exist yet)")

        # Backburner restart recovery (docs/backburner-plan.md): detached
        # tasks died with the process (rows failed above); settle their
        # still-active goals so they stop riding every prompt, waking each
        # conversation to own the loss.
        try:
            from bob_server.services.backburner import BackburnerService
            await BackburnerService(app_ctx).recover_orphaned_goals()
        except Exception:
            logger.warning("Backburner recovery sweep failed", exc_info=True)

        # Clean up stale voice sessions (bridges are gone after restart)
        try:
            from bob_server.services.voice_session_service import VoiceSessionService
            await VoiceSessionService(app_ctx).cleanup_stale()
        except Exception:
            logger.debug("Voice session cleanup skipped (table may not exist yet)")

        # Ensure the self-bob singleton exists so self-relevant claims have a target
        try:
            from bob_server.services.memory.service import MemoryService
            await MemoryService(app_ctx).ensure_self_entity()
        except Exception:
            logger.exception("Failed to ensure self-bob entity on startup")

        event_bus = EventBus()
        app_ctx.event_bus = event_bus
        app.state.event_bus = event_bus

        # Conditional WhatsApp bridge service
        wa_bridge_service = None
        if resolved_settings.whatsapp_bridge.enabled:
            try:
                from bob_server.services.whatsapp_bridge_service import WhatsAppBridgeService
                wa_bridge_service = WhatsAppBridgeService(app_ctx)
                await wa_bridge_service.start()
                app.state.whatsapp_bridge_service = wa_bridge_service
                app_ctx.whatsapp_bridge = wa_bridge_service
                logger.info("WhatsApp bridge service started")
            except Exception:
                logger.exception("WhatsApp bridge service failed to start")

        stop_event = asyncio.Event()
        runner = HeartbeatRunner(app_ctx, interval_seconds=resolved_settings.heartbeat_interval_seconds)
        runner.register(EmailPollingTask())
        runner.register(EmailSyncTask())
        runner.register(CallCleanupTask())
        runner.register(LlmLogRetentionTask())
        runner.register(EventLogReconciliationTask())
        runner.register(AttentionShadowAgreementTask())
        runner.register(EffectPumpTask())
        runner.register(ClaimRouterSweepTask())
        runner.register(OutreachDetectorSweepTask())
        runner.register(GoalReviewTask())
        runner.register(DeletionPropagationTask())
        runner.register(GrowthMonitoringTask())
        runner.register(SessionIdleSummaryTask())
        runner.register(LLMCallStalenessTask())
        runner.register(LocationFetchTask())
        runner.register(MemoryReconciliationTask())
        runner.register(DreamTask())
        heartbeat_worker = asyncio.create_task(runner.run_loop(stop_event))

        # Wakeup pump on its own short loop: it is the time-critical
        # scheduler (routine fire-times, goal deadlines) and must never sit
        # behind a slow heartbeat task. 2026-08-29: a gate-blocked idle
        # summary stalled the sequential cycle 28 min and a 17:00 feature
        # set aired late. claim_due is CAS, so a fast loop can't double-fire.
        async def _wakeup_pump_loop() -> None:
            pump = WakeupPumpTask()
            while not stop_event.is_set():
                try:
                    await pump.run(app_ctx)
                except Exception:
                    logger.exception("wakeup pump cycle failed")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=20.0)
                except asyncio.TimeoutError:
                    pass

        wakeup_pump_worker = asyncio.create_task(_wakeup_pump_loop())
        try:
            yield
        finally:
            stop_event.set()

            heartbeat_worker.cancel()
            wakeup_pump_worker.cancel()
            for worker in (heartbeat_worker, wakeup_pump_worker):
                try:
                    await worker
                except asyncio.CancelledError:
                    pass

            if wa_bridge_service is not None:
                await wa_bridge_service.stop()

            await database.close()

    # Create FastAPI app
    app = FastAPI(
        title="Bob Data Service",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # API token gate for state-changing requests. Registered before the
    # correlation middleware so CorrelationId stays outermost and 401s still
    # get correlation IDs. add_middleware prepends, so this one runs inside it.
    app.add_middleware(ApiAuthMiddleware, settings=resolved_settings)

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

    app.include_router(calendars.router)
    app.include_router(context.router)

    app.include_router(webhooks.router, prefix="/api/v1/webhooks")
    app.include_router(contacts.router, prefix="/api/v1")
    app.include_router(persona.router, prefix="/api/v1")
    app.include_router(email.router)
    # Public (Funnel) design-file publishing for the printful skill —
    # token-gated, images/print files only.
    app.include_router(published_files.router)

    # Dashboard API (HTTP) + WebSocket (live events)
    app.include_router(dashboard_api.router, prefix="/dashboard")
    app.include_router(dashboard_ws.router, prefix="/dashboard")

    # Dashboard SPA static files (must be last dashboard-related mount)
    dashboard_dist = Path(__file__).parent / "ui_dist"
    if dashboard_dist.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/dashboard", StaticFiles(directory=str(dashboard_dist), html=True), name="dashboard_spa")
        logger.info("Dashboard SPA mounted from %s", dashboard_dist)

    # Conditional voice chat router
    if resolved_settings.voice.enabled:
        from bob_server.routers import voice as voice_router
        app.include_router(voice_router.router, prefix="/voice")
        voice_router.mount_frontend(app, resolved_settings.voice.frontend_dir)

    # Conditional phone/telephony router (requires voice)
    if resolved_settings.phone.enabled:
        from bob_server.routers import phone as phone_router
        app.include_router(phone_router.router, prefix="/phone")

    # Conditional WhatsApp bridge router
    if resolved_settings.whatsapp_bridge.enabled:
        app.include_router(whatsapp.router)

    # Conditional OpenAI evaluation router
    if resolved_settings.openai.enabled:
        try:
            from bob_server.routers import openai_llm as openai_router
            app.include_router(openai_router.router)
        except ImportError:
            logger.warning("OpenAI SDK not installed — install with: pip install bob-server[openai]")

    return app
