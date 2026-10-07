"""Scenario helpers: scripted counterparts and bounded pump loops."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

_ASKING = ("?", "want me to", "shall i", "should i", "ok to", "okay to",
           "good to go", "go ahead", "approve", "confirm")


def asks(text: str) -> bool:
    t = (text or "").lower().strip()
    return t.endswith("?") or any(p in t for p in _ASKING)


async def run_until(w: Any, done: Callable[[], bool], *, ticks: int = 6,
                    step: timedelta = timedelta(minutes=15),
                    owner_answers: str | None = None) -> int:
    """Pump the world (due effects + wakeups pulled forward by ``step``)
    until ``done()`` or ``ticks`` rounds. When ``owner_answers`` is set and
    Bob's latest message to the owner is a question, the owner replies with
    it — the scripted human in the loop. Returns rounds used."""
    seen = len(w.sent_to(w.owner))
    for i in range(ticks):
        if done():
            return i
        to_owner = w.sent_to(w.owner)
        if owner_answers and len(to_owner) > seen and asks(to_owner[-1]):
            seen = len(to_owner)
            await w.inbound(w.owner, owner_answers)
            continue
        await w.tick(advance=step)
    return ticks
