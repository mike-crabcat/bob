"""Detach-probe replay: would feeding the turn's reasoning make better holds?

For each recent live detach_probe call, rebuild the probe's view at the
moment it ran — the logged transcript and called tools, plus the target
turn's reasoning_part rows that existed by then (llm_trace_events) — and
run the reasoning-aware prompt (backburner.build_probe_prompt) against the
live probe model. Prints old vs new holding text side by side.

Read-only: bob.db is opened mode=ro, calls go straight through
OpenAIService (transport only), nothing is logged or sent.

    uv run python scripts/probe_reasoning_replay.py [-n 50] [--days 14] [--out FILE]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
from pathlib import Path
from typing import Any

from server.config import Settings
from server.context import AppContext
from server.repositories.llm_trace import LlmTraceRepository
from server.services.backburner import _parse_probe_output, build_probe_prompt, probe_model
from server.services.openai_service import OpenAIService

_TOOLS_RE = re.compile(r"actually called so far: (.*?)\. Never", re.S)
# Words that would mean the notes leaked policy/persona/mechanics into chat.
_LEAK_RE = re.compile(
    r"\b(bot|ai|a\.i\.|instruction|rule|policy|system|prompt|persona|tool|"
    r"bash|subagent|goal_|add_goal|steer)\b", re.I)


class _RoDb:
    """The two async reads LlmTraceRepository needs, over a read-only sqlite."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    async def fetch_all(self, q: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(q, params).fetchall()


def load_cases(conn: sqlite3.Connection, n: int, days: int) -> list[dict[str, Any]]:
    probes = conn.execute(
        """SELECT created_at, session_key, system_prompt, user_message, response_text
           FROM llm_call_log
           WHERE call_category = 'detach_probe' AND status = 'completed'
             AND created_at >= datetime('now', ?)
             AND user_message IS NOT NULL AND system_prompt IS NOT NULL
           ORDER BY created_at DESC LIMIT ?""",
        (f"-{days} days", n)).fetchall()
    cases: list[dict[str, Any]] = []
    for p in probes:
        old = _parse_probe_output(p["response_text"])
        m = _TOOLS_RE.search(p["system_prompt"] or "")
        if old is None or m is None:
            continue
        target = conn.execute(
            """SELECT dispatch_id FROM llm_call_log
               WHERE session_key = ? AND call_category = 'whatsapp_incoming'
                 AND created_at <= ? AND dispatch_id IS NOT NULL
               ORDER BY created_at DESC LIMIT 1""",
            (p["session_key"], p["created_at"])).fetchone()
        if target is None:
            continue
        cases.append({
            "at": p["created_at"], "session_key": p["session_key"] or "",
            "dispatch_id": target["dispatch_id"],
            "tools": [t.strip() for t in m.group(1).split(",") if t.strip()],
            "transcript": (p["user_message"] or "").removeprefix("## In-flight turn\n"),
            "old": old,
        })
    return cases


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=50)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    settings = Settings.from_env()
    svc = OpenAIService(AppContext(db=None, settings=settings))  # type: ignore[arg-type]  # transport only
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    traces = LlmTraceRepository(_RoDb(conn))
    bot = settings.patience.bot_name or "Bob"
    model = probe_model(settings)

    cases = load_cases(conn, args.n, args.days)
    lines = [f"# Detach-probe replay — {len(cases)} live probes, model {model}", ""]
    stats = {"with_reasoning": 0, "changed": 0, "parse_fail": 0, "leak_flag": 0}
    for i, c in enumerate(cases, 1):
        reasoning = await traces.recent_reasoning(c["dispatch_id"], before=c["at"])
        user_line = c["transcript"].splitlines()[0][:200] if c["transcript"] else ""
        lines += [f"## {i}. {c['at']} · {c['session_key'][-30:]}", f"> {user_line}", "",
                  f"- **old hold:** {c['old']['holding_text']}"]
        if not reasoning:
            lines += ["- **new hold:** _(no reasoning by probe time — prompt unchanged)_", ""]
            continue
        stats["with_reasoning"] += 1
        system, user = build_probe_prompt(
            bot=bot, tool_names=c["tools"], transcript=c["transcript"], reasoning=reasoning)
        try:
            raw = await svc.prompt(
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                model=model, temperature=0.5, max_tokens=250, call_meta={})
        except Exception as e:  # noqa: BLE001 — a failed call is a data point
            raw = ""
            lines.append(f"- **new hold:** _ERROR {type(e).__name__}: {str(e)[:120]}_")
        new = _parse_probe_output(raw)
        if new is None:
            stats["parse_fail"] += 1
            lines += [f"- **new hold:** _unparseable_: `{(raw or '')[:160]}`", ""]
            continue
        if new["holding_text"] != c["old"]["holding_text"]:
            stats["changed"] += 1
        leak = _LEAK_RE.findall(new["holding_text"])
        if leak:
            stats["leak_flag"] += 1
        lines += [f"- **new hold:** {new['holding_text']}"
                  + (f"  ⚠ check: {', '.join(sorted(set(w.lower() for w in leak)))}" if leak else ""),
                  f"- notes at probe time: _{reasoning[:240].replace(chr(10), ' ')}…_", ""]
        print(f"[{i}/{len(cases)}] done", flush=True)

    lines[1:1] = ["", f"reasoning available: {stats['with_reasoning']}/{len(cases)} · "
                      f"parse failures: {stats['parse_fail']} · "
                      f"leak-word flags: {stats['leak_flag']}", ""]
    report = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(report)
        print(f"wrote {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    asyncio.run(main())
