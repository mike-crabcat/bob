"""Live-session occupancy state machine (Bob3 Phase VI item 6).

While a realtime voice call is live on a person's conversation, inbound
text on the SAME conversation queues for the post-call turn by default:
the message is stored (undispatched) at ingress as usual, but the
attention dispatch is skipped; when the call ends, the drain callback
(`WhatsAppBridgeService.wake_session`) re-arms dispatch so the queued
messages run as one post-call turn.

States per conversation_id: IDLE (absent from the table) or LIVE
(a call is in progress, keyed by call_ref = subagent id).

Escape hatches, per the plan:

* **Urgent text** bypasses queueing — messages matching `_URGENT_RE`
  ("urgent", "emergency", "hang up", "stop the call", …) dispatch
  immediately even while the call is live, so the operator can always
  interrupt.
* **Maximum occupancy** — at most ``MAX_LIVE_CALLS`` calls live at once;
  ``mark_live`` raises ``OccupancyError`` beyond that, so a runaway LLM
  can't fan out phone calls.
* **TTL** — a LIVE entry older than ``LIVE_TTL_S`` is treated as stale
  (e.g. a voice link that was never tapped, or a crash mid-call), so a
  conversation can never queue forever.

State is in-memory: calls do not survive a server restart, and the
recovery sweep already re-arms any undispatched messages on startup.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

MAX_LIVE_CALLS = 2
LIVE_TTL_S = 3600.0

_URGENT_RE = re.compile(
    r"\b(urgent|emergency|hang\s*up|stop\s+the\s+call|end\s+the\s+call|"
    r"call\s+off|abort|000|911|112)\b",
    re.IGNORECASE,
)

# conversation_id -> {"ref": call_ref, "since": monotonic ts, "deferred": bool}
_live: dict[str, dict[str, Any]] = {}
_drain: Callable[[str], Awaitable[None]] | None = None


class OccupancyError(RuntimeError):
    """Raised when MAX_LIVE_CALLS would be exceeded."""


def set_drain(fn: Callable[[str], Awaitable[None]] | None) -> None:
    """Register the post-call drain (wake_session); called by the WA service."""
    global _drain
    _drain = fn


def reset_for_tests() -> None:
    global _drain
    _live.clear()
    _drain = None


def _expire_stale() -> None:
    now = time.monotonic()
    for cid in [c for c, e in _live.items() if now - e["since"] > LIVE_TTL_S]:
        logger.warning("occupancy: expiring stale live entry for %s", cid)
        _live.pop(cid, None)


def mark_live(conversation_id: str, call_ref: str) -> None:
    """Enter LIVE. Raises OccupancyError past MAX_LIVE_CALLS."""
    _expire_stale()
    if conversation_id in _live:
        _live[conversation_id].update(ref=call_ref, since=time.monotonic())
        return
    if len(_live) >= MAX_LIVE_CALLS:
        raise OccupancyError(
            f"maximum live-call occupancy reached ({MAX_LIVE_CALLS}); "
            f"finish a call before placing another")
    _live[conversation_id] = {"ref": call_ref, "since": time.monotonic(),
                              "deferred": False}
    logger.info("occupancy: %s LIVE (call %s)", conversation_id, call_ref[:8])


def is_live(conversation_id: str) -> bool:
    _expire_stale()
    return conversation_id in _live


def live_count() -> int:
    _expire_stale()
    return len(_live)


def is_urgent(text: str) -> bool:
    return bool(_URGENT_RE.search(text or ""))


def defer(conversation_id: str) -> None:
    """Record that an inbound message was queued during the live call."""
    entry = _live.get(conversation_id)
    if entry is not None:
        entry["deferred"] = True


def mark_idle(conversation_id: str) -> None:
    """Leave LIVE; if messages were deferred, fire the drain callback."""
    entry = _live.pop(conversation_id, None)
    if entry is None:
        return
    logger.info("occupancy: %s IDLE (call %s)", conversation_id, entry["ref"][:8])
    if entry.get("deferred") and _drain is not None:
        async def _run(cid: str = conversation_id) -> None:
            try:
                await _drain(cid)  # type: ignore[misc]
            except Exception:
                logger.warning("occupancy: drain failed for %s", cid, exc_info=True)
        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            logger.warning("occupancy: no running loop; drain skipped for %s",
                           conversation_id)


def mark_idle_by_ref(call_ref: str) -> None:
    """Idle whichever conversation is live under this call_ref (used by
    failure paths that only know the subagent id)."""
    for cid, entry in list(_live.items()):
        if entry["ref"] == call_ref:
            mark_idle(cid)
            return
