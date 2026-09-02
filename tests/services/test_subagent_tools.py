"""Subagent tool gating and session ownership.

Untrusted sessions (group chats, untrusted DM contacts) get script-only
subagents: the skill index advertises create_subagent to every session as
the async path for slow scripts (image gen, browser runs), and a script
subagent runs in the same workspace sandbox as the bash tool the session
already has. LLM-loop (claude/local) and phone (openai_voice) subagents
spend tokens or place real calls and stay trusted-only; message_subagent
(which drives further LLM runs) is withheld from untrusted sessions
entirely.

check/kill/message are session-scoped at the service layer: one session
must not be able to inspect, drive, or kill another session's subagents.

Regression context (2026-08-27): untrusted sessions had NO create_subagent,
so image-gen requests fell back to blocking bash runs (30-90s conversation
stalls) — the exact anti-pattern the skill docs forbid.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

GROUP_KEY = "agent:main:whatsapp:group:111"  # group routes are untrusted
DM_KEY = "agent:main:whatsapp:dm:61400000001"

NOW = "2026-08-27T02:00:00+00:00"


def _tools(ctx, session_key, is_trusted):
    from bob_server.services.subagent_tools import make_subagent_tools
    return {t.name: t for t in
            make_subagent_tools(ctx, session_key, is_trusted=is_trusted)}


def test_untrusted_toolset_has_no_message_subagent(ctx):
    names = set(_tools(ctx, GROUP_KEY, is_trusted=False))
    assert {"create_subagent", "check_subagent", "list_subagents",
            "kill_subagent"} <= names
    assert "message_subagent" not in names


def test_trusted_toolset_is_complete(ctx):
    names = set(_tools(ctx, DM_KEY, is_trusted=True))
    assert {"create_subagent", "check_subagent", "message_subagent",
            "list_subagents", "kill_subagent"} <= names


async def test_untrusted_create_subagent_rejects_non_script(ctx):
    """Voice/LLM types (and invented aliases) must fail before the service's
    alias normalisation can coerce them into a phone call or LLM run."""
    tools = _tools(ctx, GROUP_KEY, is_trusted=False)
    for bad in ("claude", "local", "openai_voice", "script-executor"):
        with patch("bob_server.services.subagent_service.SubagentService") as svc_cls:
            out = json.loads(await tools["create_subagent"].handler(
                task="do a thing", agent_type=bad))
        assert not out["ok"], bad
        assert "untrusted" in out["error"], bad
        svc_cls.assert_not_called()


async def test_untrusted_create_subagent_allows_script(ctx):
    tools = _tools(ctx, GROUP_KEY, is_trusted=False)
    with patch("bob_server.services.subagent_service.SubagentService") as svc_cls:
        svc_cls.return_value.create_subagent = AsyncMock(
            return_value={"ok": True, "subagent_id": "sa-1"})
        out = json.loads(await tools["create_subagent"].handler(
            task="python skills/openai-image/openai_image.py --prompt x",
            agent_type=" Script "))
    assert out["ok"]
    call = svc_cls.return_value.create_subagent.await_args
    assert call.kwargs["agent_type"] == " Script "


async def test_check_and_kill_scoped_to_parent_session(ctx):
    """Another session's subagent is invisible: not-found, never confirmed."""
    from bob_server.services.subagent_service import SubagentService

    await ctx.db.execute(
        """INSERT INTO subagents (id, parent_session_key, session_key, task,
                                 status, agent_type, created_at, updated_at)
           VALUES ('sa-1', ?, 'subagent:%s:sa-1', 'gen image',
                   'waiting_for_parent', 'script', ?, ?)""" % DM_KEY,
        (DM_KEY, NOW, NOW))

    svc = SubagentService(ctx)
    own = await svc.check_subagent("sa-1", parent_session_key=DM_KEY)
    assert own["ok"] and own["status"] == "waiting_for_parent"

    other = await svc.check_subagent("sa-1", parent_session_key=GROUP_KEY)
    assert not other["ok"] and other["error"] == "Subagent not found"

    denied = await svc.kill_subagent("sa-1", parent_session_key=GROUP_KEY)
    assert not denied["ok"]

    # Untouched by the denied kill:
    survivor = await svc.check_subagent("sa-1", parent_session_key=DM_KEY)
    assert survivor["status"] == "waiting_for_parent"


async def test_registry_gives_untrusted_sessions_subagent_tools(ctx):
    """The registry wires the (script-gated) tools whenever skill dev is on —
    that is the production flag — and nothing when it is off."""
    from bob_server.services.tool_registry import build_common_tools

    ctx.settings.harness.skill_dev_enabled = True
    try:
        names = {t.name for t in build_common_tools(
            ctx, session_key=GROUP_KEY, is_trusted=False, include_routines=False)}
        assert "create_subagent" in names
        assert "message_subagent" not in names

        trusted_names = {t.name for t in build_common_tools(
            ctx, session_key=DM_KEY, is_trusted=True, include_routines=False)}
        assert "message_subagent" in trusted_names
    finally:
        ctx.settings.harness.skill_dev_enabled = False

    off_names = {t.name for t in build_common_tools(
        ctx, session_key=DM_KEY, is_trusted=True, include_routines=False)}
    assert "create_subagent" not in off_names
