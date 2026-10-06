"""Shared helpers for eval cases (Phase 1 harness, 2026-09-28).

`extract_tool_calls` was promoted from `record_discipline.py` so every
category reads tool activity the same way; the model pin lets
`bob eval run --model <slug>` hold the serving model constant across
pre/post comparisons (the rotation pool behaves very differently —
GLM-flash vs Opus — and an unpinned battery compares apples to
oranges). Cases pass ``model=pinned_model()`` to run_turn; None
means "resolve as usual".
"""

from __future__ import annotations

import pathlib
from typing import Any

_pinned_model: str | None = None


def set_pinned_model(model: str | None) -> None:
    """Pin (or clear) the serving model for subsequent case runs."""
    global _pinned_model
    _pinned_model = (model or "").strip() or None


def pinned_model() -> str | None:
    """The pinned serving model, or None when the runner left it free."""
    return _pinned_model


def extract_tool_calls(messages: list[Any]) -> list[dict[str, Any]]:
    """Every tool call in a post-dispatch messages list, both API shapes.

    Chat-completions ``tool_calls`` on assistant messages AND Responses-API
    ``function_call`` items (what run_turn actually appends — the
    chat-completions branch alone made tool_call_made fail even when calls
    fired; Phase 0 eval-harness fix 2026-09-19).
    """
    calls: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                calls.append({
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                })
        if msg.get("type") == "function_call":
            calls.append({
                "name": msg.get("name", ""),
                "arguments": msg.get("arguments", ""),
            })
    return calls


def make_planted_bash(files: dict, *, cwd_subdir: str = ""):
    """A bash mock that REALLY EXECUTES in a planted temp tree.

    The inert '(mock) command accepted; no output' mock poisoned the
    first delegation baseline: models investigated, got dead output, and
    spent turns reporting broken tooling instead of routing (2026-09-28).
    Here bash is a real subprocess in a /tmp fixture dir — ls/grep/cat
    return genuine results against the planted files, writes land in
    /tmp (never the live workspace), and the tree is removed on cleanup.

    Returns (bash_tool, cleanup_coro_fn).
    """
    import asyncio
    import shutil
    import tempfile

    from server.services.tools import tool

    root = tempfile.mkdtemp(prefix="bob-eval-fx-")
    for rel, content in (files or {}).items():
        path = pathlib.Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    cwd = str(pathlib.Path(root) / cwd_subdir) if cwd_subdir else root

    @tool
    async def bash(command: str) -> str:
        """Run a bash command in the workspace directory. The workspace is the cwd, so relative paths land there. The skill environment (BOB_* vars) is inherited. Output above 30000 chars is truncated — use head/tail/sed -n/grep to page through large files. Times out after 900s.

        Bob's Python venv at ~/bobenv is active — `python` and `pip` resolve there, and `pip install <pkg>` lands in ~/bobenv (shared across all skills; skills do not get their own venvs).

        Do NOT write files under memory/ with this tool — use memory_write instead, or the memory index (claims, entities) will not pick them up.

        SANDBOX: The workspace is the only allowed directory. Reaching outside it (DB clients, /etc, /home/bob/data, /home/bob/config, ~, .., sudo, secrets) is blocked. Use memory_*/contact_*/group_*/docs_* tools for data outside the workspace — do not try to bypass blocks via subshells, python, or symlinks."""
        # The fixture IS the world (2026-10-06): absolute paths used to
        # reach the live workspace and ~/data — a fan-out case spent 86
        # rounds mining real figurine history and an archived DB dump.
        # Same sandbox filter as Bob's real bash, plus no /home/bob at all.
        from server.services.workspace_tools import _check_command_safety
        blocked = _check_command_safety(
            command, db_path=None, data_dir=pathlib.Path("/home/bob/data"),
            config_dir=pathlib.Path("/home/bob/config"))
        if blocked:
            return blocked
        if "/home/bob" in command or "~/" in command:
            return ("BLOCKED: path outside the workspace. The workspace is "
                    "the cwd — use relative paths.")
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            return "Error: command timed out after 15s"
        text = out.decode(errors="replace")[:4000]
        return text or "(no output)"

    async def cleanup() -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, shutil.rmtree, root, True)

    return bash, cleanup


def make_shadow_surface(real_tools: list, exclude: set[str] = frozenset()):
    """Real tool names/descriptions/schemas with INERT handlers — the
    production-sized tool crowd without execution.

    Fixture fidelity (2026-09-29, report §14): the delegation cases ran
    with 7 visible tools against production's ~46, making the routing
    tools far more salient than in a real turn — measured compliance was
    the best case. Shadows restore the crowd; functional mocks (bash,
    create_subagent, run_bg_process, the send) ride alongside, replacing
    their real twins by name via ``exclude``."""
    import json as _json

    from server.services.tools import Tool

    shadows: list = []
    for t in real_tools:
        if t.name in exclude:
            continue

        async def _inert(_name=t.name, **kwargs):
            # Reads that shape replies get a realistic empty; everything
            # else a plain success. Never "(mock)"-flavoured — deepseek
            # already smells fixtures and discounts answers over them.
            if _name in ("recall", "find", "search_session_messages",
                         "find_session", "get_session_messages"):
                return _json.dumps({"results": [], "matches": []})
            return _json.dumps({"ok": True})

        shadows.append(Tool(name=t.name, description=t.description,
                            parameters=t.parameters, required=t.required,
                            handler=_inert))
    return shadows
