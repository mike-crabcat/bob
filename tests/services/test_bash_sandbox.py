"""Landlock bash sandbox (2026-10-12): the real kernel, through the real
bash tool. Skipped where Landlock is unavailable."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from server.services import bash_sandbox
from server.services.workspace_tools import make_workspace_tools

needs_landlock = pytest.mark.skipif(
    bash_sandbox.landlock_abi() < 1, reason="Landlock unavailable here")


@pytest.fixture
def world(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    secret = tmp_path / "config"
    secret.mkdir()
    (secret / ".env").write_text("TWILIO_TOKEN=s3cret\n")
    settings = SimpleNamespace(
        harness=SimpleNamespace(workspace_dir=ws, venv_dir=tmp_path / "bobenv"),
        db_path=tmp_path / "data" / "bob.db", data_dir=tmp_path / "data",
        config_dir=secret, port=8420)
    return SimpleNamespace(ws=ws, secret=secret, settings=settings,
                           ctx=SimpleNamespace(settings=settings))


async def _bash(ctx, command: str) -> str:
    tools = {t.name: t.handler for t in make_workspace_tools(ctx)}
    return await tools["bash"](command)


def test_off_mode_leaves_argv_untouched(monkeypatch, world):
    monkeypatch.setenv("BOB_BASH_SANDBOX", "off")
    assert bash_sandbox.wrap(["bash", "-c", "true"], world.settings) == \
        ["bash", "-c", "true"]


def test_policy_never_allows_bobs_own_port(monkeypatch, world):
    monkeypatch.setenv("BOB_BASH_SANDBOX_PORTS", "443,8420,9223")
    pol = bash_sandbox.policy(world.settings)
    assert 8420 not in pol["tcp_connect"] and 443 in pol["tcp_connect"]
    assert str(world.ws.resolve()) in pol["rw"]
    assert not any(str(world.secret) in p for p in pol["rw"] + pol["ro"])


@needs_landlock
async def test_workspace_writes_work_and_outside_reads_fail(monkeypatch, world):
    monkeypatch.setenv("BOB_BASH_SANDBOX", "landlock")
    out = await _bash(world.ctx, "echo hi > note.txt && cat note.txt")
    assert out == "hi"
    # Outside the policy: the kernel refuses, whatever the command shape.
    # (Paths built at runtime so the regex preflight can't be what stops it.)
    out = await _bash(world.ctx,
                      f"p={world.secret}; cat \"$p/.env\"")
    assert "Permission denied" in out and "s3cret" not in out
    out = await _bash(world.ctx, f"d={world.ws.parent}; echo x > \"$d/escape\"")
    assert "Permission denied" in out
    assert not (world.ws.parent / "escape").exists()


@needs_landlock
async def test_bob_service_signals_are_scoped(monkeypatch, world):
    import os
    monkeypatch.setenv("BOB_BASH_SANDBOX", "landlock")
    # The test process stands in for bob.service: outside the sandbox.
    out = await _bash(world.ctx, f"kill -0 {os.getpid()}")
    assert "not permitted" in out.lower()


@needs_landlock
async def test_symlink_out_of_workspace_is_still_denied(monkeypatch, world):
    monkeypatch.setenv("BOB_BASH_SANDBOX", "landlock")
    (world.ws / "cfg").symlink_to(world.secret)
    out = await _bash(world.ctx, "cat cfg/.env")
    assert "Permission denied" in out and "s3cret" not in out


def test_launcher_exits_125_on_bad_policy():
    import subprocess
    r = subprocess.run([bash_sandbox.LAUNCHER_PYTHON, "-I", "-S",
                        str(bash_sandbox.LAUNCHER), "--policy", "{not json",
                        "--", "true"], capture_output=True, text=True)
    assert r.returncode == 125 and "landlock:" in r.stderr


@needs_landlock
async def test_service_secrets_are_not_in_the_shell_env(monkeypatch, world):
    monkeypatch.setenv("BOB_BASH_SANDBOX", "landlock")
    monkeypatch.setenv("BOB_TWILIO_AUTH_TOKEN", "tw1l10-s3cret")
    monkeypatch.setenv("BOB_OPENAI_API_KEY", "sk-mapped-ok")
    out = await _bash(world.ctx, "env")
    assert "tw1l10-s3cret" not in out
    assert "OPENAI_API_KEY=sk-mapped-ok" in out       # mapped skill key kept
    assert "TMPDIR=/tmp/bob-sandbox" in out
    assert "/bob/.venv" not in out          # service PATH not inherited


@needs_landlock
async def test_unix_socket_connects_are_refused(monkeypatch, world):
    """Landlock can't govern pathname unix-socket connects on this kernel;
    the seccomp layer must (systemd-run --user / docker / tailscale)."""
    import socket
    srv_path = world.ws / "s.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(srv_path))
    srv.listen(1)
    try:
        monkeypatch.setenv("BOB_BASH_SANDBOX", "landlock")
        out = await _bash(world.ctx, (
            "python3 -c \"import socket\ntry:\n s=socket.socket(socket.AF_UNIX)"
            "\n s.connect('s.sock'); print('CONNECTED')\nexcept OSError as e: "
            "print('DENIED', e.errno)\""))
        assert out.strip() == "DENIED 13", out
        # socketpair (multiprocessing, pipes) still works.
        out = await _bash(world.ctx, "python3 -c \"import socket; "
                          "a,b=socket.socketpair(); a.send(b'x'); print(b.recv(1))\"")
        assert "b'x'" in out
    finally:
        srv.close()


def test_service_manager_hint_matches_command_positions_only():
    from server.services.workspace_tools import _service_manager_hint
    assert _service_manager_hint("systemctl --user start bg-x.service")
    assert _service_manager_hint("ls; systemd-run --user true")
    assert _service_manager_hint("journalctl --user -u bob.service | tail")
    assert "bg_start" in _service_manager_hint("sudo systemctl restart x")
    # mentions in arguments/files are not calls
    assert _service_manager_hint("grep systemctl skills/x/skill.md") is None
    assert _service_manager_hint("echo 'use systemctl'") is None


@needs_landlock
async def test_systemctl_gets_the_bg_tools_hint(monkeypatch, world):
    monkeypatch.setenv("BOB_BASH_SANDBOX", "landlock")
    out = await _bash(world.ctx, "systemctl --user start bg-aus-legal.service")
    assert "bg_start" in out and "use_skill" in out
