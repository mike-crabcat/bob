"""Stimulus router — drains pending stimulus_events into steers.

Runs every heartbeat tick (docs/stimulus-spine-plan.md, Part 1):
  pending rows → TTL-expire stale → match stimulus_routes →
  batch per target session → ONE steer per target per tick → mark processed.

Failure semantics: the steer is created before rows are marked, so a crash
re-delivers a batch; dedup_key absorbs source-side retries and the steer's
own content makes re-delivery visible rather than confusing. Worst case is
late, never lost.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from server.context import AppContext
from server.repositories.stimulus import StimulusRepository

logger = logging.getLogger(__name__)

# Procesed rows older than this are pruned (events are not a ledger; the
# delivered_steer pointer keeps the audit trail's meaning).
PRUNE_AFTER_DAYS = 30


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def match_route(event: dict[str, Any],
                routes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """First enabled route matching source/type/level, ordered by
    (priority, id) — sorted here so callers can pass routes in any order."""
    for r in sorted(routes, key=lambda r: (r.get("priority") or 0, r.get("id") or 0)):
        if r["source"] not in ("*", event["source"]):
            continue
        if not fnmatch.fnmatchcase(event["type"], r["type_pattern"]):
            continue
        if r["level"] not in ("*", event["level"]):
            continue
        return r
    return None


def render_instruction(events: list[dict[str, Any]]) -> str:
    """The steer content for one batch. The silent-decline line is
    mandatory — alerts must be declinable without manufacturing chatter."""
    lines: list[str] = []
    for e in events:
        head = f"[Stimulus: {e['source']} {e['type']}"
        if e.get("dedup_key"):
            head += f", dedup {e['dedup_key']}"
        head += "]"
        lines.append(head)
        if e.get("summary"):
            lines.append(str(e["summary"]))
    lines.append(
        "If you act on this, do it with the platform's tools and report here; "
        "if not, no reply is needed.")
    return "\n".join(lines)


async def drain(ctx: AppContext) -> dict[str, int]:
    """One router pass. Returns counts for the heartbeat log line."""
    repo = StimulusRepository(ctx.db)
    pending = await repo.pending_events()
    if not pending:
        return {"pending": 0, "expired": 0, "steered": 0, "logged": 0}

    now = datetime.now(timezone.utc)
    routes = await repo.routes(enabled_only=True)

    expired_ids: list[int] = []
    log_only_ids: list[int] = []
    batches: dict[str, list[dict[str, Any]]] = {}
    for e in pending:
        ttl = e.get("ttl_s")
        ts = _parse_ts(e.get("ts") or "")
        if ttl is not None and ts is not None and now > ts + timedelta(seconds=ttl):
            expired_ids.append(e["id"])
            continue
        if e["level"] != "action":
            log_only_ids.append(e["id"])  # info never wakes anyone
            continue
        route = match_route(e, routes)
        if route is None or not route.get("target_session"):
            log_only_ids.append(e["id"])  # unrouted or log-only route
            continue
        batches.setdefault(route["target_session"], []).append(e)

    await repo.mark_processed(expired_ids, "expired")
    await repo.mark_processed(log_only_ids, "log-only")

    steered = 0
    for target, events in sorted(batches.items()):
        from server.services.wake_service import wake_conversation
        try:
            armed = await wake_conversation(
                ctx, target, render_instruction(events),
                call_category="steer",
                metadata={"stimulus_ids": [e["id"] for e in events],
                          "stimulus_sources": sorted({e["source"] for e in events})},
                provenance="steer")
        except Exception:
            logger.exception("stimulus steer failed for %s (ids %s)",
                             target, [e["id"] for e in events])
            continue  # leave pending — retried next tick
        outcome = "steer:ok" if armed else "steer:undispatched"
        await repo.mark_processed([e["id"] for e in events], outcome)
        steered += len(events)
        logger.info("stimulus: steered %d event(s) -> %s (%s)",
                    len(events), target, outcome)

    # opportunistic prune (cheap; volume is tiny)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    try:
        await repo.prune_processed_before(cutoff)
    except Exception:
        logger.exception("stimulus prune failed (non-fatal)")

    return {"pending": len(pending), "expired": len(expired_ids),
            "steered": steered, "logged": len(log_only_ids)}
