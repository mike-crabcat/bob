"""Tests for DispatchService — record, track, complete, cancel, tap."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from cyborg_server.context import AppContext
from cyborg_server.database import Database
from cyborg_server.models import DispatchStatus
from cyborg_server.services.base import utcnow
from cyborg_server.services.dispatch_service import DispatchService


async def _record(
    ctx: AppContext,
    *,
    notification_type: str = "email_incoming",
    session_key: str = "test-session",
    notification_id: str | None = None,
    task_id: str | None = None,
    project_id: str | None = None,
) -> str:
    """Shortcut to record a dispatch."""
    svc = DispatchService(ctx)
    return await svc.record_dispatch(
        notification_type=notification_type,
        session_key=session_key,
        notification_id=notification_id,
        task_id=task_id,
        project_id=project_id,
    )


async def _insert_notification(db: Database, notification_id: str) -> None:
    """Insert a minimal notification row to satisfy FK constraints."""
    now = utcnow().isoformat()
    await db.execute(
        """
        INSERT INTO notifications
            (id, entity_type, entity_id, notification_type, status, title, message, created_at, updated_at)
        VALUES (?, 'task', ?, 'needs_input', 'pending', 'test', 'test', ?, ?)
        """,
        (notification_id, notification_id, now, now),
    )


# ---------------------------------------------------------------------------
# record_dispatch
# ---------------------------------------------------------------------------


async def test_record_dispatch_returns_id(ctx: AppContext):
    dispatch_id = await _record(ctx)
    assert dispatch_id

    svc = DispatchService(ctx)
    dispatch = await svc.get_dispatch(dispatch_id)
    assert dispatch is not None
    assert dispatch.status == DispatchStatus.ACTIVE
    assert dispatch.notification_type == "email_incoming"
    assert dispatch.session_key == "test-session"


async def test_record_dispatch_with_optional_fields(ctx: AppContext):
    await _insert_notification(ctx.db, "aaaaaaaa-1111-2222-3333-444444444444")

    svc = DispatchService(ctx)
    dispatch_id = await svc.record_dispatch(
        notification_type="task_assignment",
        session_key="proj-abc",
        notification_id="aaaaaaaa-1111-2222-3333-444444444444",
        task_id="bbbbbbbb-1111-2222-3333-444444444444",
        project_id="cccccccc-1111-2222-3333-444444444444",
    )
    dispatch = await svc.get_dispatch(dispatch_id)
    assert dispatch is not None
    assert dispatch.notification_type == "task_assignment"
    assert str(dispatch.notification_id) == "aaaaaaaa-1111-2222-3333-444444444444"
    assert str(dispatch.task_id) == "bbbbbbbb-1111-2222-3333-444444444444"
    assert str(dispatch.project_id) == "cccccccc-1111-2222-3333-444444444444"


async def test_record_dispatch_cancels_prior_active(ctx: AppContext):
    """Recording a new dispatch for the same notification_id cancels the prior one."""
    await _insert_notification(ctx.db, "aaaaaaaa-1111-2222-3333-444444444444")

    dispatch_id_1 = await _record(ctx, notification_id="aaaaaaaa-1111-2222-3333-444444444444", session_key="s1")
    dispatch_id_2 = await _record(ctx, notification_id="aaaaaaaa-1111-2222-3333-444444444444", session_key="s2")

    svc = DispatchService(ctx)
    first = await svc.get_dispatch(dispatch_id_1)
    second = await svc.get_dispatch(dispatch_id_2)

    assert first.status == DispatchStatus.CANCELLED
    assert second.status == DispatchStatus.ACTIVE


async def test_record_dispatch_no_notification_does_not_cancel(ctx: AppContext):
    """Dispatches without notification_id don't cancel each other."""
    d1 = await _record(ctx, session_key="s1")
    d2 = await _record(ctx, session_key="s2")

    svc = DispatchService(ctx)
    assert (await svc.get_dispatch(d1)).status == DispatchStatus.ACTIVE
    assert (await svc.get_dispatch(d2)).status == DispatchStatus.ACTIVE


# ---------------------------------------------------------------------------
# _complete
# ---------------------------------------------------------------------------


async def test_complete_marks_finished_with_duration(ctx: AppContext):
    dispatch_id = await _record(ctx)
    svc = DispatchService(ctx)
    await svc._complete(dispatch_id)

    dispatch = await svc.get_dispatch(dispatch_id)
    assert dispatch.status == DispatchStatus.COMPLETED
    assert dispatch.completed_at is not None
    assert dispatch.duration_seconds is not None
    assert dispatch.duration_seconds >= 0


async def test_complete_idempotent_on_already_completed(ctx: AppContext):
    dispatch_id = await _record(ctx)
    svc = DispatchService(ctx)
    await svc._complete(dispatch_id)
    await svc._complete(dispatch_id)

    dispatch = await svc.get_dispatch(dispatch_id)
    assert dispatch.status == DispatchStatus.COMPLETED


# ---------------------------------------------------------------------------
# _update_status
# ---------------------------------------------------------------------------


async def test_update_status_to_failed(ctx: AppContext):
    dispatch_id = await _record(ctx)
    svc = DispatchService(ctx)
    await svc._update_status(dispatch_id, DispatchStatus.FAILED)

    dispatch = await svc.get_dispatch(dispatch_id)
    assert dispatch.status == DispatchStatus.FAILED
    assert dispatch.completed_at is not None


async def test_update_status_to_timed_out(ctx: AppContext):
    dispatch_id = await _record(ctx)
    svc = DispatchService(ctx)
    await svc._update_status(dispatch_id, DispatchStatus.TIMED_OUT)

    dispatch = await svc.get_dispatch(dispatch_id)
    assert dispatch.status == DispatchStatus.TIMED_OUT


# ---------------------------------------------------------------------------
# track
# ---------------------------------------------------------------------------


async def test_track_completes_on_success(ctx: AppContext):
    dispatch_id = await _record(ctx)
    svc = DispatchService(ctx)

    completed = asyncio.Event()

    async def _succeed():
        completed.set()

    svc.track(dispatch_id, _succeed())
    await asyncio.sleep(0.2)

    assert completed.is_set()
    dispatch = await svc.get_dispatch(dispatch_id)
    assert dispatch.status == DispatchStatus.COMPLETED


async def test_track_marks_failed_on_exception(ctx: AppContext):
    dispatch_id = await _record(ctx)
    svc = DispatchService(ctx)

    async def _fail():
        raise RuntimeError("boom")

    svc.track(dispatch_id, _fail())
    await asyncio.sleep(0.2)

    dispatch = await svc.get_dispatch(dispatch_id)
    assert dispatch.status == DispatchStatus.FAILED


async def test_track_marks_timed_out_on_timeout_error(ctx: AppContext):
    dispatch_id = await _record(ctx)
    svc = DispatchService(ctx)

    async def _timeout():
        raise asyncio.TimeoutError()

    svc.track(dispatch_id, _timeout())
    await asyncio.sleep(0.2)

    dispatch = await svc.get_dispatch(dispatch_id)
    assert dispatch.status == DispatchStatus.TIMED_OUT


# ---------------------------------------------------------------------------
# complete_dispatches_for_task / project
# ---------------------------------------------------------------------------


async def test_complete_dispatches_for_task(ctx: AppContext):
    d1 = await _record(ctx, task_id="aaaaaaaa-1111-2222-3333-444444444444", session_key="s1")
    d2 = await _record(ctx, task_id="aaaaaaaa-1111-2222-3333-444444444444", session_key="s2")
    d_other = await _record(ctx, task_id="bbbbbbbb-1111-2222-3333-444444444444", session_key="s3")

    svc = DispatchService(ctx)
    count = await svc.complete_dispatches_for_task("aaaaaaaa-1111-2222-3333-444444444444")
    assert count == 2

    assert (await svc.get_dispatch(d1)).status == DispatchStatus.COMPLETED
    assert (await svc.get_dispatch(d2)).status == DispatchStatus.COMPLETED
    assert (await svc.get_dispatch(d_other)).status == DispatchStatus.ACTIVE


async def test_complete_dispatches_for_project(ctx: AppContext):
    d1 = await _record(ctx, project_id="aaaaaaaa-1111-2222-3333-444444444444", session_key="s1")
    d_other = await _record(ctx, project_id="bbbbbbbb-1111-2222-3333-444444444444", session_key="s2")

    svc = DispatchService(ctx)
    count = await svc.complete_dispatches_for_project("aaaaaaaa-1111-2222-3333-444444444444")
    assert count == 1

    assert (await svc.get_dispatch(d1)).status == DispatchStatus.COMPLETED
    assert (await svc.get_dispatch(d_other)).status == DispatchStatus.ACTIVE


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


async def test_cancel_dispatch(ctx: AppContext):
    dispatch_id = await _record(ctx)
    svc = DispatchService(ctx)

    result = await svc.cancel_dispatch(dispatch_id)
    assert result is not None
    assert result.status == DispatchStatus.CANCELLED

    # Already cancelled — second call returns None
    assert await svc.cancel_dispatch(dispatch_id) is None


async def test_cancel_nonexistent_dispatch(ctx: AppContext):
    svc = DispatchService(ctx)
    assert await svc.cancel_dispatch("does-not-exist") is None


# ---------------------------------------------------------------------------
# cancel_active_dispatches
# ---------------------------------------------------------------------------


async def test_cancel_active_dispatches(ctx: AppContext):
    await _record(ctx, session_key="s1")
    await _record(ctx, session_key="s2")

    svc = DispatchService(ctx)
    count = await svc.cancel_active_dispatches()
    assert count == 2

    assert await svc.count_active_dispatches() == 0


# ---------------------------------------------------------------------------
# list_active_dispatches
# ---------------------------------------------------------------------------


async def test_list_active_dispatches(ctx: AppContext):
    await _record(ctx, session_key="s1")
    await _record(ctx, session_key="s2")
    d3 = await _record(ctx, session_key="s3")

    svc = DispatchService(ctx)
    await svc._complete(d3)

    active = await svc.list_active_dispatches()
    assert len(active) == 2
    assert all(d.status == DispatchStatus.ACTIVE for d in active)


# ---------------------------------------------------------------------------
# get_stuck_dispatches
# ---------------------------------------------------------------------------


async def test_get_stuck_dispatches_finds_old(ctx: AppContext):
    dispatch_id = await _record(ctx, session_key="s1")

    two_hours_ago = (utcnow() - timedelta(hours=2)).isoformat()
    await ctx.db.execute(
        "UPDATE dispatches SET dispatched_at = ? WHERE id = ?",
        (two_hours_ago, dispatch_id),
    )

    svc = DispatchService(ctx)
    stuck = await svc.get_stuck_dispatches(timeout_minutes=60.0)
    assert len(stuck) == 1
    assert stuck[0].session_key == "s1"


async def test_get_stuck_dispatches_skips_recent(ctx: AppContext):
    await _record(ctx, session_key="s1")

    svc = DispatchService(ctx)
    stuck = await svc.get_stuck_dispatches(timeout_minutes=60.0)
    assert len(stuck) == 0


# ---------------------------------------------------------------------------
# count_active_dispatches
# ---------------------------------------------------------------------------


async def test_count_active_dispatches(ctx: AppContext):
    assert await DispatchService(ctx).count_active_dispatches() == 0
    await _record(ctx, session_key="s1")
    await _record(ctx, session_key="s2")
    assert await DispatchService(ctx).count_active_dispatches() == 2


# ---------------------------------------------------------------------------
# get_dispatch
# ---------------------------------------------------------------------------


async def test_get_dispatch_nonexistent(ctx: AppContext):
    svc = DispatchService(ctx)
    assert await svc.get_dispatch("does-not-exist") is None


# ---------------------------------------------------------------------------
# wait_for_active_dispatches
# ---------------------------------------------------------------------------


async def test_wait_returns_immediately_when_none_active(ctx: AppContext):
    svc = DispatchService(ctx)
    await svc.wait_for_active_dispatches(timeout_seconds=5.0, poll_interval=0.1)


async def test_wait_cancels_after_timeout(ctx: AppContext):
    d1 = await _record(ctx, session_key="s1")
    svc = DispatchService(ctx)
    await svc.wait_for_active_dispatches(timeout_seconds=0.3, poll_interval=0.1)

    assert (await svc.get_dispatch(d1)).status == DispatchStatus.CANCELLED


# ---------------------------------------------------------------------------
# tap_dispatch
# ---------------------------------------------------------------------------


async def test_tap_dispatch_increments_count(ctx: AppContext):
    task_id = "aaaaaaaa-1111-2222-3333-444444444444"
    dispatch_id = await _record(ctx, task_id=task_id, session_key="s1")
    svc = DispatchService(ctx)

    with patch("cyborg_server.services.notification_service.NotificationService.create_task_tap_notification", new_callable=AsyncMock) as mock_tap:
        result = await svc.tap_dispatch(dispatch_id)

    assert result is not None
    assert result.tap_count == 1
    mock_tap.assert_awaited_once_with(task_id, now=mock_tap.call_args.kwargs["now"])


async def test_tap_dispatch_returns_none_if_not_active(ctx: AppContext):
    dispatch_id = await _record(ctx, session_key="s1")
    svc = DispatchService(ctx)
    await svc._complete(dispatch_id)

    assert await svc.tap_dispatch(dispatch_id) is None


async def test_tap_dispatch_returns_none_without_task_id(ctx: AppContext):
    dispatch_id = await _record(ctx, session_key="s1")
    svc = DispatchService(ctx)

    assert await svc.tap_dispatch(dispatch_id) is None


async def test_tap_dispatch_returns_none_for_unknown_id(ctx: AppContext):
    svc = DispatchService(ctx)
    assert await svc.tap_dispatch("does-not-exist") is None
