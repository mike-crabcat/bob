"""Sleep guard: heredoc bodies written to files are data (2026-10-09 —
writing a script containing `sleep 3600` was refused twice)."""

from server.services.workspace_tools import _inline_sleep_seconds as longest


def test_script_written_via_heredoc_is_not_a_sleep():
    cmd = ("cd /w && cat > s.sh <<'EOF'\n#!/bin/bash\nwhile true; do\n"
           "  sleep 3600\ndone\nEOF\nchmod +x s.sh")
    assert longest(cmd) is None


def test_real_sleeps_still_caught():
    assert longest("sleep 60") == 60
    assert longest("echo hi && sleep 30") == 30
    assert longest("cat <<EOF > a\nsleep 5\nEOF\nsleep 20") == 20


def test_heredoc_fed_to_a_shell_still_runs():
    assert longest("bash <<'EOF'\nsleep 99\nEOF") == 99
