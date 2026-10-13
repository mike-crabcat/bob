"""Crash recovery retires pending rows for sessions with no route instead
of re-warning at every boot (2026-10-13)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from server.services.whatsapp_bridge_service import WhatsAppBridgeService

PHANTOM = "agent:main:whatsapp:dm:6145406854"


async def test_no_route_rows_are_retired_not_woken(ctx):
    await ctx.db.execute(
        "INSERT INTO messages (id, conversation_id, role, content, channel, "
        "provenance, dispatched, created_at) VALUES "
        "('p1', ?, 'user', '[Report from goal room]', 'whatsapp', 'wake_nudge', 0, "
        "datetime('now'))", (PHANTOM,))
    svc = WhatsAppBridgeService(ctx)
    svc.wake_session = AsyncMock()
    assert await svc.resume_pending_sessions() == 0
    svc.wake_session.assert_not_awaited()
    row = await ctx.db.fetch_one("SELECT dispatched FROM messages WHERE id = 'p1'")
    assert row["dispatched"] == 1
    assert await svc.resume_pending_sessions() == 0       # nothing left to retry
