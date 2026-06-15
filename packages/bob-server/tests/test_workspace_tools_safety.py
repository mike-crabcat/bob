"""Unit tests for the bash sandbox guardrail in workspace_tools.

Run with: uv run pytest packages/bob-server/bob_server/services/workspace_tools_safety_test.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bob_server.services.workspace_tools import _check_command_safety


@pytest.fixture
def paths() -> dict[str, Path]:
    return {
        "db_path": Path("/home/bob/data/bob.db"),
        "data_dir": Path("/home/bob/data"),
        "config_dir": Path("/home/bob/config"),
    }


def _check(cmd: str, paths: dict[str, Path]) -> str | None:
    return _check_command_safety(
        cmd,
        db_path=paths["db_path"],
        data_dir=paths["data_dir"],
        config_dir=paths["config_dir"],
    )


@pytest.mark.parametrize("cmd", [
    'sqlite3 /home/bob/data/bob.db "SELECT * FROM contacts"',
    'sqlite3 ~/data/bob.db ".tables"',
    'psql -d bob -c "SELECT 1"',
    "mysql -u root bob",
    "mariadb bob",
    "duckdb /tmp/x.db",
    'python -c "import sqlite3; sqlite3.connect(\'bob.db\')"',
    'echo "select 1" | sqlite3 /home/bob/data/bob.db',
])
def test_blocks_db_clients(cmd: str, paths: dict[str, Path]) -> None:
    reason = _check(cmd, paths)
    assert reason is not None, f"should block: {cmd}"
    assert "database client" in reason.lower() or "memory" in reason.lower()


@pytest.mark.parametrize("cmd", [
    "cat /home/bob/data/bob.db",
    "ls -la /home/bob/data/bob.db",
    "cp bob.db /tmp/",
    "echo $BOB_DB_PATH",
    "BOB_DB_PATH=/x bob.db",
])
def test_blocks_db_file_references(cmd: str, paths: dict[str, Path]) -> None:
    reason = _check(cmd, paths)
    assert reason is not None, f"should block: {cmd}"


@pytest.mark.parametrize("cmd", [
    "cat /etc/passwd",
    "cat /root/.bashrc",
    "ls /var/lib/",
    "cat /proc/self/environ",
    "cat ~/.ssh/id_rsa",
    "cat ~/.aws/credentials",
    "cat ~/.config/bob/config.toml",
    "cat credentials.json",
    "source .env",
])
def test_blocks_sensitive_paths(cmd: str, paths: dict[str, Path]) -> None:
    reason = _check(cmd, paths)
    assert reason is not None, f"should block: {cmd}"


@pytest.mark.parametrize("cmd", [
    "ls /home/bob/data/",
    "ls /home/bob/data",
    "ls /home/bob/config/",
    "cat /home/bob/config/secrets.toml",
])
def test_blocks_data_and_config_dirs(cmd: str, paths: dict[str, Path]) -> None:
    reason = _check(cmd, paths)
    assert reason is not None, f"should block: {cmd}"


@pytest.mark.parametrize("cmd", [
    "cd .. && ls",
    "cd ../.. && cat /etc/passwd",
    "cat ../secret.txt",
    "ls ../../etc",
])
def test_blocks_traversal(cmd: str, paths: dict[str, Path]) -> None:
    reason = _check(cmd, paths)
    assert reason is not None, f"should block: {cmd}"


@pytest.mark.parametrize("cmd", [
    "sudo rm -rf /",
    "sudo apt install evil",
    "su root -c 'rm -rf /'",
    "pkexec bash",
    "doas cat /etc/shadow",
])
def test_blocks_privilege_escalation(cmd: str, paths: dict[str, Path]) -> None:
    reason = _check(cmd, paths)
    assert reason is not None, f"should block: {cmd}"


@pytest.mark.parametrize("cmd", [
    "ls",
    "ls -la",
    "git status",
    "git log --oneline -10",
    "git diff HEAD~3",
    "cat README.md",
    "echo hello > scratch/note.txt",
    "python scripts/resize_image.py photos/cat.jpg",
    "uv run python -m pytest tests/",
    'grep -rn "foo" packages/',
    'find . -name "*.py" | head',
    "mkdir -p reports/2026-06",
    "head -50 skills/foo/skill.md",
    "tail -100 /home/bob/workspace/logs/thing.log",
    "sed -n '100,200p' reports/june.md",
    "awk '{print $1}' data.csv",
    "curl https://api.github.com/users/octocat",
    "wget https://example.com/file -O scratch/file",
])
def test_allows_legitimate_workspace_ops(cmd: str, paths: dict[str, Path]) -> None:
    reason = _check(cmd, paths)
    assert reason is None, f"should allow but got: {reason!r} for cmd: {cmd}"
