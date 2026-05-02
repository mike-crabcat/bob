"""Registerable background tasks for the heartbeat loop."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

from cyborg_server.config import Settings
from cyborg_server.context import AppContext
from cyborg_server.database import Database


logger = logging.getLogger(__name__)


@runtime_checkable
class HeartbeatTask(Protocol):
    """Protocol for background tasks that run on each heartbeat cycle."""

    name: str

    async def run(self, ctx: AppContext) -> None: ...


class HeartbeatRunner:
    """Runs registered heartbeat tasks on a fixed interval."""

    def __init__(self, ctx: AppContext, *, interval_seconds: float) -> None:
        self._ctx = ctx
        self._interval = interval_seconds
        self._tasks: list[HeartbeatTask] = []

    def register(self, task: HeartbeatTask) -> None:
        self._tasks.append(task)

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        """Run all registered tasks on each cycle until stopped."""
        if self._interval <= 0:
            await stop_event.wait()
            return

        cycle = 0
        while not stop_event.is_set():
            cycle += 1
            for task in self._tasks:
                # Email sync runs every 10 cycles
                if isinstance(task, EmailSyncTask) and cycle % 10 != 0:
                    continue
                try:
                    await task.run(self._ctx)
                except Exception:
                    logger.exception("Heartbeat task %s failed", task.name)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
            except TimeoutError:
                continue


class NotificationDispatchTask:
    """Dispatch pending notifications on each heartbeat cycle."""

    name = "notification_dispatch"

    async def run(self, ctx: AppContext) -> None:
        from cyborg_server.services.notification_service import NotificationService

        await NotificationService(ctx).dispatch_pending()


class BlockedProjectCheckTask:
    """Find blocked projects missing notifications and raise one."""

    name = "blocked_project_check"

    async def run(self, ctx: AppContext) -> None:
        from cyborg_server.models import ProjectState
        from cyborg_server.services.notification_service import NotificationService

        blocked = await ctx.db.fetch_all(
            """SELECT id FROM projects
               WHERE deleted_at IS NULL AND state = ? AND blocked_reason IS NOT NULL""",
            (ProjectState.PAUSED.value,),
        )
        notification_service = NotificationService(ctx)
        for project in blocked:
            await notification_service.sync_project_state(project["id"])


class EmailPollingTask:
    """Poll AgentMail inboxes for new email messages."""

    name = "email_polling"

    async def run(self, ctx: AppContext) -> None:
        settings = ctx.settings
        if not settings.agentmail.enabled or not settings.email_polling_enabled:
            return

        from cyborg_server.services.agentmail_client import AgentMailClient
        from cyborg_server.services.email_polling_service import EmailPollingService

        client = AgentMailClient(
            base_url=settings.agentmail.base_url,
            api_key=settings.agentmail.api_key,
        )
        try:
            service = EmailPollingService(ctx, agentmail_client=client)
            count = await service.poll_all_inboxes()
            if count > 0:
                logger.info("Email polling processed %d new message(s)", count)
        finally:
            await client.close()


class StuckDispatchCheckTask:
    """Log stuck dispatches that have been active beyond the timeout threshold."""

    name = "stuck_dispatch_check"

    async def run(self, ctx: AppContext) -> None:
        from cyborg_server.services.dispatch_service import DispatchService

        dispatch_service = DispatchService(ctx)
        stuck = await dispatch_service.get_stuck_dispatches(
            timeout_minutes=ctx.settings.dispatch_stuck_timeout_minutes,
        )
        if stuck:
            logger.warning(
                "Found %d stuck dispatch(es) older than %.0f minutes",
                len(stuck), ctx.settings.dispatch_stuck_timeout_minutes,
            )


class EmailSyncTask:
    """Periodic full email sync — reconcile AgentMail with local database."""

    name = "email_sync"

    async def run(self, ctx: AppContext) -> None:
        settings = ctx.settings
        if not settings.agentmail.enabled:
            return

        from cyborg_server.services.agentmail_client import AgentMailClient
        from cyborg_server.services.email_polling_service import EmailPollingService

        client = AgentMailClient(
            base_url=settings.agentmail.base_url,
            api_key=settings.agentmail.api_key,
        )
        try:
            service = EmailPollingService(ctx, agentmail_client=client)
            count = await service.sync_all_inboxes()
            if count > 0:
                logger.info("Periodic email sync persisted %d missing message(s)", count)
        finally:
            await client.close()
