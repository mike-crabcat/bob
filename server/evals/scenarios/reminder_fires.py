"""Capability: schedule → wake fires → handled.

"Remind me tomorrow at 9am…" must become a real schedule at the right
local time (the 2026-09 incidents: UTC hours written into local-hour
contracts, prose times delaying wakes), and when the schedule fires the
reminder must actually reach Mike."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from server.evals.case import Check
from server.evals.registry import eval_case

PERTH = ZoneInfo("Australia/Perth")


@eval_case(
    id="cap_reminder_fires_and_lands",
    category="capability",
    tier="capability",
    description="A reminder for tomorrow 9am is scheduled at the right local "
                "time and, when it fires, reaches Mike.",
    checks=[Check("flag", {"key": "scheduled_at_9am_local"}),
            Check("flag", {"key": "reminder_delivered"})],
    rubric=["The message that reached Mike when the schedule fired reads as the "
            "reminder (call the plumber about the hot water system), not a "
            "confused or meta message."],
    context="Timezone Australia/Perth (UTC+8). The world fast-forwards "
            "schedules due before tomorrow 09:30 local.",
    timeout_seconds=600,
)
async def cap_reminder_fires_and_lands(w):
    from server.repositories.wakeups import WakeupRepository
    await w.inbound("Mike", "remind me tomorrow at 9am to call the plumber about "
                    "the hot water system")
    local_now = w.now.astimezone(PERTH)
    target = (local_now + timedelta(days=1)).replace(hour=9, minute=0, second=0,
                                                     microsecond=0)
    rows = await WakeupRepository(w.db).list_all_scheduled(limit=200)
    when = []
    for r in rows:
        try:
            t = datetime.fromisoformat(str(r["not_before"]).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=ZoneInfo("UTC"))
            when.append(t)
        except ValueError:
            continue
    w.flag("scheduled", [t.astimezone(PERTH).isoformat() for t in when])
    w.flag("scheduled_at_9am_local", any(abs((t - target).total_seconds()) <= 15 * 60
                                         for t in when))
    before = len(w.sent_to("Mike"))
    horizon = (target + timedelta(minutes=30)) - datetime.now(PERTH)
    await w.tick(advance=horizon)
    fired = w.sent_to("Mike")[before:]
    w.flag("reminder_delivered", any("plumber" in t.lower() for t in fired))
    return {"response": "\n".join(fired) or "(nothing reached Mike when the schedule fired)"}
