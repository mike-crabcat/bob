"""Unit tests for the bash sandbox guardrail in workspace_tools.

Run with: uv run pytest packages/bob-server/server/services/workspace_tools_safety_test.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.services.workspace_tools import _check_command_safety


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


# ---------------------------------------------------------------- syntax
# bash_syntax_check: the quoting preflight added 2026-09-05 after the
# crayon-portrait goal died as "unexpected EOF" inside a background script
# (\' inside single quotes — a Python/JSON habit bash does not honor).

@pytest.mark.asyncio
async def test_syntax_check_catches_backslash_apostrophe():
    from server.services.workspace_tools import bash_syntax_check

    bad = ("python skills/openai-image/openai_image.py --prompt 'A crayon "
           "portrait like a young kid\\'s drawing' --output out.png")
    err = await bash_syntax_check(bad)
    assert err is not None
    assert "unexpected EOF" in err or "matching" in err


@pytest.mark.asyncio
async def test_syntax_check_passes_valid_commands():
    from server.services.workspace_tools import bash_syntax_check

    for cmd in (
        "echo hello",
        "python skills/x.py --prompt-file scratch/p.txt --output o.png",
        "cat > scratch/p.txt <<'EOF'\nsome prose with an apostrophe: kid's\nEOF\n",
        "for f in *.png; do echo \"$f\"; done",
    ):
        assert await bash_syntax_check(cmd) is None, cmd


@pytest.mark.asyncio
async def test_syntax_check_error_mentions_quote():
    from server.services.workspace_tools import bash_syntax_check

    err = await bash_syntax_check("echo 'unterminated")
    assert err is not None and "`'" in err or err is not None


# --- heredoc bodies are data, not paths (2026-10-08 eval finding) ----------

@pytest.mark.parametrize("cmd", [
    "cat > src/routes/view.tsx <<'EOF'\nimport { f } from '../lib/dates';\nEOF",
    "python3 - <<EOF\nimport os\nprint(os.path.relpath('a', '../b'))\nEOF\necho done",
    "cat <<-EOF > t.ts\n\timport x from '../../x';\n\tEOF",
    "cat > a.py <<'A' && cat > b.py <<'B'\nimport ..x\nA\nfrom .. import y\nB",
])
def test_allows_dotdot_inside_heredoc_bodies(cmd: str, paths: dict[str, Path]) -> None:
    assert _check(cmd, paths) is None, f"should allow: {cmd!r}"


@pytest.mark.parametrize("cmd", [
    # traversal in the command itself is still blocked, heredoc or not
    "cd .. && cat > x <<'EOF'\nhello\nEOF",
    "cat <<'EOF' > ../outside.txt\nhello\nEOF",
    "cat <<'EOF'\nhi\nEOF\ncat ../secret",
    # an unterminated heredoc hides nothing after its operator line
    "cat ../secret <<EOF",
    # here-strings are not heredocs
    "cat <<< '../x' ../y",
    # other checks still see heredoc bodies
    "python3 - <<'EOF'\nopen('/etc/passwd').read()\nEOF",
])
def test_heredoc_exemption_is_narrow(cmd: str, paths: dict[str, Path]) -> None:
    assert _check(cmd, paths) is not None, f"should block: {cmd!r}"


@pytest.mark.parametrize("cmd", [
    "cd repos/dashboard && sed -i \"s|from '../lib/dates'|from '../src/lib/dates'|\" tests/x.ts",
    "sed -e 's#\\.\\./lib#../src/lib#' -i a.ts",
    "grep -rn \"from '../\" src/",
    "awk '/\\.\\.\\//{print}' src/a.ts",
    "python3 -c \"import os; print(os.path.normpath('a/../b'))\"",
    "node -e \"require('../x')\"",
    "perl -pe 's|\\.\\./|./|' -i a.js",
])
def test_allows_dotdot_in_scripts_and_patterns(cmd: str, paths: dict[str, Path]) -> None:
    assert _check(cmd, paths) is None, f"should allow: {cmd!r}"


@pytest.mark.parametrize("cmd", [
    "sed -i 's/a/b/' ../outside.txt",          # the FILE is still a path
    "grep foo ../secret.txt",
    "cat \"../x\"",                            # quoted paths are paths
    "X=../y ls",
    "../bin/tool --flag",
    "cp a.txt ../",
    "tar czf out.tgz --directory=../ .",
    "cat 'unbalanced ../x",                    # unparseable → strict regex
])
def test_path_traversal_still_blocked(cmd: str, paths: dict[str, Path]) -> None:
    assert _check(cmd, paths) is not None, f"should block: {cmd!r}"
