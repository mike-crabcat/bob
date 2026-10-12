"""KPI repository — read-only daily aggregates across messages and
llm_call_log (the `self kpi` tool, services/self_tools.py).

A human inbound message is a ``role='user'`` row on a main conversation
(``agent:main:…``) with a ``sender_id`` and no provenance — that excludes
steers, routines, wakes, task relays, bg placeholders, reactions
(``wa_reaction``) and the WhatsApp history import. LLM calls exclude the
eval harness (``eval``/``eval_judge`` categories and ``eval:`` sessions),
which logs into the live table. Days are server-local calendar days;
timestamps are stored UTC.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

_HUMAN_INBOUND = (
    "role = 'user' AND conversation_id LIKE 'agent:main:%' "
    "AND sender_id IS NOT NULL AND provenance IS NULL")
_NOT_EVAL = (
    "COALESCE(call_category, '') NOT LIKE 'eval%' AND COALESCE(session_key, '') NOT LIKE 'eval:%'")


def _local_offset_modifier() -> str:
    off = datetime.now().astimezone().utcoffset() or timedelta(0)
    return f"{int(off.total_seconds() // 60):+d} minutes"


class KpiRepository:
    def __init__(self, db: Any):
        self.db = db

    async def daily(self, days: int) -> list[dict[str, Any]]:
        """One row per local day, oldest first, today last (partial).
        Keys: day, active_users, active_groups, active_dms, messages_received,
        llm_calls."""
        mod = _local_offset_modifier()
        today = datetime.now().astimezone().date()
        first = today - timedelta(days=days - 1)
        # Lower bound in UTC with a day of slack; the local-date filter is exact.
        since = (datetime.combine(first, datetime.min.time())
                 - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        msgs = await self.db.fetch_all(
            f"SELECT date(created_at, ?) AS day, "
            f"COUNT(DISTINCT sender_id) AS active_users, "
            f"COUNT(DISTINCT CASE WHEN conversation_id LIKE '%:group:%' "
            f"THEN conversation_id END) AS active_groups, "
            f"COUNT(DISTINCT CASE WHEN conversation_id LIKE '%:dm:%' "
            f"THEN conversation_id END) AS active_dms, "
            f"COUNT(*) AS messages_received "
            f"FROM messages WHERE {_HUMAN_INBOUND} AND created_at >= ? "
            f"GROUP BY day", (mod, since))
        calls = await self.db.fetch_all(
            f"SELECT date(created_at, ?) AS day, COUNT(*) AS llm_calls "
            f"FROM llm_call_log WHERE {_NOT_EVAL} AND created_at >= ? GROUP BY day", (mod, since))
        by_day: dict[str, dict[str, Any]] = {}
        for i in range(days):
            d = (first + timedelta(days=i)).isoformat()
            by_day[d] = {"day": d, "active_users": 0, "active_groups": 0,
                         "active_dms": 0, "messages_received": 0, "llm_calls": 0}
        for r in msgs:
            if r["day"] in by_day:
                by_day[r["day"]].update(
                    active_users=r["active_users"], active_groups=r["active_groups"],
                    active_dms=r["active_dms"],
                    messages_received=r["messages_received"])
        for r in calls:
            if r["day"] in by_day:
                by_day[r["day"]]["llm_calls"] = r["llm_calls"]
        return list(by_day.values())

    async def distinct_users(self, first: date) -> int:
        """Distinct human senders since local day ``first`` (window total —
        not the sum of daily counts)."""
        mod = _local_offset_modifier()
        since = (first - timedelta(days=1)).isoformat()
        row = await self.db.fetch_one(
            f"SELECT COUNT(DISTINCT sender_id) AS n FROM messages "
            f"WHERE {_HUMAN_INBOUND} AND created_at >= ? AND date(created_at, ?) >= ?",
            (since, mod, first.isoformat()))
        return int(row["n"]) if row else 0
