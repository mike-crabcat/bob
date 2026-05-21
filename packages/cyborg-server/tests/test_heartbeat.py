"""Tests for heartbeat tasks and runner."""

from __future__ import annotations

import asyncio

import pytest

from cyborg_server.config import Settings
from cyborg_server.context import AppContext
from cyborg_server.database import Database
from cyborg_server.heartbeat import (
    BlockedProjectCheckTask,
    EmailSyncTask,
    HeartbeatRunner,
    NotificationDispatchTask,
    StuckDispatchCheckTask,
)


async def test_runner_stops_on_event(ctx: AppContext):
    """Runner exits immediately when stop_event is already set."""
    runner = HeartbeatRunner(ctx, interval_seconds=60)
    runner.register(NotificationDispatchTask())

    stop = asyncio.Event()
    stop.set()

    await runner.run_loop(stop)
    # If we get here, the runner respected the stop event


async def test_runner_zero_interval_waits_forever(ctx: AppContext):
    """Runner with interval <= 0 just waits for the stop event."""
    runner = HeartbeatRunner(ctx, interval_seconds=0)
    stop = asyncio.Event()

    async def _stop_after_delay():
        await asyncio.sleep(0.1)
        stop.set()

    asyncio.create_task(_stop_after_delay())
    await runner.run_loop(stop)


async def test_stuck_dispatch_check_no_stuck(ctx: AppContext):
    """StuckDispatchCheckTask completes without error when no dispatches exist."""
    task = StuckDispatchCheckTask()
    await task.run(ctx)


async def test_blocked_project_check_no_blocked(ctx: AppContext):
    """BlockedProjectCheckTask completes without error when no projects exist."""
    task = BlockedProjectCheckTask()
    await task.run(ctx)


async def test_email_sync_skips_when_disabled(ctx: AppContext):
    """EmailSyncTask exits early when agentmail is not enabled."""
    task = EmailSyncTask()
    await task.run(ctx)


async def test_notification_dispatch_no_pending(ctx: AppContext):
    """NotificationDispatchTask completes without error when no notifications exist."""
    task = NotificationDispatchTask()
    await task.run(ctx)
