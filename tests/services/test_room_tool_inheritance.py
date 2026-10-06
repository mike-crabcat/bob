"""Goal rooms inherit the chat toolset under their creator's principal,
minus outward-acting tools (2026-10-06): rooms had no recall, no
background jobs, no subagents."""

from __future__ import annotations

import pytest

from server.services import wake_service

ROOM = "agent:goal-abcdef12-0000-0000-0000-000000000000:utility"


async def _names(ctx, monkeypatch, trusted):
    async def _principal(ctx, key):
        return trusted, None
    monkeypatch.setattr("server.services.goal_rooms.room_creator_principal", _principal)
    tools = await wake_service.room_inherited_tools(ctx, ROOM, {"bash"})
    return {t.name for t in tools}


async def test_trusted_room_gets_memory_jobs_subagents(ctx, monkeypatch):
    names = await _names(ctx, monkeypatch, True)
    for want in ("recall", "find", "remember", "run_bg_process", "create_subagent",
                 "search_contacts"):
        assert want in names, want
    for withheld in ("email_send", "write_routine", "delete_routine", "create_contact"):
        assert withheld not in names, withheld
    assert "bash" not in names, "existing room tools keep their own version"


async def test_untrusted_room_gets_the_narrower_set(ctx, monkeypatch):
    trusted = await _names(ctx, monkeypatch, True)
    untrusted = await _names(ctx, monkeypatch, False)
    assert "recall" in untrusted
    assert untrusted < trusted, "an untrusted creator's room gets strictly less"
