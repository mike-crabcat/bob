"""The `self` tool — a small CLI onto Bob's own internals.

One tool, a subcommand string: the schema cost stays flat as subcommands
are added, and ``self help`` documents itself. Runs in-process (DB,
session, outbox), unlike bash, which is sandboxed away from the data.
Models invent subcommands and flags, so a bad command returns usage text
plus a hint, never a bare error.

Subcommands:
  kpi — daily engagement + LLM usage (counts only, never content).
        Allowed for trusted senders, and in conversations whose policy
        has ``self_kpi: true`` (e.g. Bob-management, where routines run
        untrusted).

Usage:
    tools.extend(make_self_tools(ctx, session_key=..., is_trusted=...))
"""

from __future__ import annotations

import json
import shlex
from datetime import date
from typing import Any

from server.services.tools import Tool, tool

MAX_DAYS = 365

USAGE = """\
self <subcommand> [options]

  help                      this text
  kpi [--days N] [--json] [--out FILE]
                            daily KPIs for the last N local days (default 30,
                            max 365; today is partial):
                              active users      distinct people who messaged Bob
                              active groups     groups where a person spoke
                              active dms        DM chats where the person spoke
                              messages received human messages in Bob's chats
                                                (DMs + every group message)
                              llm calls         every LLM call Bob made, incl.
                                                probes, memory and dream work
                                                (eval runs excluded)
                            --json returns the rows for charting.
                            --out FILE writes that JSON to a workspace file
                            (e.g. for a chart script) and returns a summary —
                            never copy KPI JSON into a command by hand."""

_ALIASES = {"kpis": "kpi", "stats": "kpi", "metrics": "kpi", "engagement": "kpi",
            "--help": "help", "-h": "help"}


class _UsageError(ValueError):
    pass


def _parse_kpi(args: list[str]) -> tuple[int, bool, str | None]:
    days, as_json, out = 30, False, None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--json", "json", "-j"):
            as_json = True
        elif a in ("--days", "-d", "-n", "--last"):
            if i + 1 >= len(args):
                raise _UsageError(f"{a} needs a number")
            i += 1
            days = _int(args[i])
        elif a in ("--out", "-o", "--output"):
            if i + 1 >= len(args):
                raise _UsageError(f"{a} needs a workspace file path")
            i += 1
            out = args[i]
        elif a.startswith(("--out=", "--output=")):
            out = a.split("=", 1)[1]
        elif a.startswith(("--days=", "--last=")):
            days = _int(a.split("=", 1)[1])
        elif a.rstrip("d").isdigit():          # `kpi 14`, `kpi 14d`
            days = _int(a.rstrip("d"))
        else:
            raise _UsageError(f"unknown option {a!r}")
        i += 1
    if not 1 <= days <= MAX_DAYS:
        raise _UsageError(f"--days must be 1..{MAX_DAYS}")
    return days, as_json, out


def _int(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        raise _UsageError(f"{s!r} is not a number") from None


def _render(rows: list[dict[str, Any]], window_users: int) -> str:
    cols = ("active_users", "active_groups", "active_dms", "messages_received",
            "llm_calls")
    lines = ["day         users  groups  dms  msgs_in  llm_calls"]
    for r in rows:
        lines.append(f"{r['day']}  {r['active_users']:>5}  {r['active_groups']:>6}  "
                     f"{r['active_dms']:>3}  {r['messages_received']:>7}  {r['llm_calls']:>9}")
    full = rows[:-1] or rows                   # averages skip partial today
    avg = {c: sum(r[c] for r in full) / len(full) for c in cols}
    lines.append(
        f"avg/day     {avg['active_users']:>5.1f}  {avg['active_groups']:>6.1f}  "
        f"{avg['active_dms']:>3.1f}  {avg['messages_received']:>7.1f}  {avg['llm_calls']:>9.1f}"
        f"   (full days only)")
    lines.append(
        f"window: {window_users} distinct users, "
        f"{sum(r['messages_received'] for r in rows)} messages received, "
        f"{sum(r['llm_calls'] for r in rows)} LLM calls. Last row is today so far.")
    return "\n".join(lines)


async def _allowed(ctx: Any, session_key: str, is_trusted: bool) -> bool:
    if is_trusted:
        return True
    from server.repositories.conversations import ConversationRepository
    policy = await ConversationRepository(ctx.db).get_policy(session_key)
    return bool(policy.get("self_kpi"))


def make_self_tools(ctx: Any, *, session_key: str, is_trusted: bool = False) -> list[Tool]:

    @tool
    async def self(command: str = "help") -> str:
        """Bob's own internals as a small CLI. Run `help` for subcommands.
        `kpi [--days N] [--json] [--out FILE]`: daily active users, active
        groups, active DMs, messages received and LLM calls (counts only).
        For scripts, use --out to write the JSON to a workspace file."""
        try:
            argv = shlex.split(command or "help")
        except ValueError as exc:
            return f"error: {exc}\n\n{USAGE}"
        if argv and argv[0] in ("self", "bob"):          # `self kpi` echoed back
            argv = argv[1:]
        sub = _ALIASES.get(argv[0].lower(), argv[0].lower()) if argv else "help"
        args = argv[1:]
        if sub == "help":
            return USAGE
        if sub != "kpi":
            return f"error: unknown subcommand {sub!r}\n\n{USAGE}"
        try:
            days, as_json, out = _parse_kpi(args)
        except _UsageError as exc:
            return f"error: {exc}\n\n{USAGE}"
        if not await _allowed(ctx, session_key, is_trusted):
            return ("error: kpi isn't enabled in this conversation (trusted "
                    "senders only, or the owner enables it per chat).")
        from server.repositories.kpi import KpiRepository
        repo = KpiRepository(ctx.db)
        rows = await repo.daily(days)
        window_users = await repo.distinct_users(date.fromisoformat(rows[0]["day"]))
        payload = {"days": rows, "window_distinct_users": window_users,
                   "today_partial": True}
        if out:
            from server.services.workspace_tools import _resolve_writable
            try:
                path = _resolve_writable(ctx, out)
            except ValueError as exc:
                return f"error: --out {exc}\n\n{USAGE}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=1) + "\n")
            return (f"wrote {out}: {len(rows)} days {rows[0]['day']}..{rows[-1]['day']} "
                    f"(last day is today, partial), {window_users} distinct users. "
                    f"Read the file; don't retype its contents.")
        if as_json:
            return json.dumps(payload)
        return _render(rows, window_users)

    return [self]
