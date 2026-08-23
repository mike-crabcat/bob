"""Script subagents (async skill execution).

Slow skill scripts (image gen, browser automation) must not block the
conversation turn. create_subagent(agent_type='script') runs the command as
a durable spawn effect; completion settles the linked goal, which wakes the
parent conversation with the output (see subagent_service._notify_parent).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bob_server.services.subagent_service import SubagentService, _running_tasks


async def _wait_for_status(db, subagent_id, statuses, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        row = await db.fetch_one(
            "SELECT status, result, error_message FROM subagents WHERE id = ?",
            (subagent_id,))
        if row and row["status"] in statuses:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"subagent never reached {statuses}: {row}")


@pytest.mark.asyncio
async def test_script_subagent_runs_and_wakes_parent(ctx, monkeypatch, tmp_path):
    """Happy path: script runs out-of-band, result wakes the parent."""
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)

    woken: list[tuple[str, str]] = []

    async def fake_wake(_ctx, conversation_id, content, **kwargs):
        woken.append((conversation_id, content))

    import bob_server.services.wake_service as wake_service
    monkeypatch.setattr(wake_service, "wake_conversation", fake_wake)

    svc = SubagentService(ctx)
    result = await svc.create_subagent(
        "echo hello-from-script", "agent:main:whatsapp:group:test",
        agent_type="script")
    assert result["status"] in ("created", "running")
    subagent_id = result["subagent_id"]

    row = await _wait_for_status(
        ctx.db, subagent_id, ("waiting_for_parent", "completed", "failed"))
    assert row["status"] == "waiting_for_parent"
    assert "hello-from-script" in row["result"]
    assert "exit_code=0" in row["result"]

    # Parent is woken via goal settle (goal_result wake) or direct wake —
    # either way the content carries the script output and send-artifact nudge.
    assert woken, "parent conversation was never woken"
    conv, content = woken[0]
    assert conv == "agent:main:whatsapp:group:test"
    assert "hello-from-script" in content
    assert "[Script" in content or "Goal completed" in content


@pytest.mark.asyncio
async def test_script_subagent_failure_wakes_parent_with_error(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)

    woken: list[str] = []

    async def fake_wake(_ctx, conversation_id, content, **kwargs):
        woken.append(content)

    import bob_server.services.wake_service as wake_service
    monkeypatch.setattr(wake_service, "wake_conversation", fake_wake)

    svc = SubagentService(ctx)
    result = await svc.create_subagent(
        "exit 3", "agent:main:whatsapp:dm:tester", agent_type="script")
    row = await _wait_for_status(ctx.db, result["subagent_id"], ("failed",))
    assert "exit_code=3" in (row["error_message"] or "")
    assert woken and "ERROR" in woken[0]


@pytest.mark.asyncio
async def test_script_subagent_sandbox_blocks_escape(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)

    async def fake_wake(_ctx, conversation_id, content, **kwargs):
        pass

    import bob_server.services.wake_service as wake_service
    monkeypatch.setattr(wake_service, "wake_conversation", fake_wake)

    svc = SubagentService(ctx)
    result = await svc.create_subagent(
        "sudo rm -rf /", "agent:main:whatsapp:dm:tester", agent_type="script")
    row = await _wait_for_status(ctx.db, result["subagent_id"], ("failed",))
    assert "sandbox" in (row["error_message"] or "").lower()


@pytest.mark.asyncio
async def test_script_with_contact_id_is_not_hijacked_to_voice(ctx, monkeypatch, tmp_path):
    """contact_id forces openai_voice for unknown types — but never for 'script'."""
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)

    async def fake_wake(_ctx, conversation_id, content, **kwargs):
        pass

    import bob_server.services.wake_service as wake_service
    monkeypatch.setattr(wake_service, "wake_conversation", fake_wake)

    svc = SubagentService(ctx)
    result = await svc.create_subagent(
        "echo ok", "agent:main:whatsapp:dm:tester",
        agent_type="script", contact_id="contact-123")
    row = await ctx.db.fetch_one(
        "SELECT agent_type FROM subagents WHERE id = ?", (result["subagent_id"],))
    assert row["agent_type"] == "script"
    await _wait_for_status(ctx.db, result["subagent_id"], ("waiting_for_parent",))
