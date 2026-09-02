"""Claude Code subprocess runs must not leak on timeout.

A timed-out skill-dev run was left alive on 2026-08-25: the orphan kept
rewriting skills/printful for 9+ minutes after SubagentService had already
recorded a failure — with a blank error message, because str(TimeoutError)
is "". These tests pin the fix across both _run_claude copies: kill the
process and raise a descriptive, resumable error.
"""

from __future__ import annotations

import asyncio

import pytest

import server.services.skill_developer_service as skill_developer_service
import server.services.subagent_service as subagent_service
from server.services.skill_developer_service import SkillDeveloperService
from server.services.subagent_service import SubagentService


class FakeProc:
    """Stands in for the claude subprocess; communicate() never finishes."""

    def __init__(self):
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(30)
        return b"", b""

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


@pytest.mark.asyncio
@pytest.mark.parametrize("make_svc", [
    lambda ctx: SubagentService(ctx),
    lambda ctx: SkillDeveloperService(ctx),
])
async def test_timeout_kills_process_and_raises_descriptive_error(
        ctx, monkeypatch, tmp_path, make_svc):
    svc = make_svc(ctx)
    fake = FakeProc()

    async def fake_exec(*args, **kwargs):
        return fake

    module = (skill_developer_service if isinstance(svc, SkillDeveloperService)
              else subagent_service)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(module.shutil, "which", lambda _: str(tmp_path / "claude"))
    (tmp_path / "claude").write_text("#!/bin/sh\n")
    monkeypatch.setattr(ctx.settings.harness, "skill_dev_timeout_seconds", 0.05)

    with pytest.raises(RuntimeError) as excinfo:
        await svc._run_claude(prompt="improve the skill", cwd=tmp_path)

    message = str(excinfo.value)
    assert "timed out after" in message
    assert "claude --resume" in message  # run stays resumable, not a dead end
    assert fake.killed, "timed-out subprocess must be killed, not orphaned"
