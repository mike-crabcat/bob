"""Stimulus router — drains pending stimulus_events into steers.

Runs every heartbeat tick (docs/stimulus-spine-plan.md, Part 1):
  pending rows → TTL-expire stale → match stimulus_routes →
  batch per target session → ONE steer per target per tick → mark processed.

Utility-conversations plan (docs/utility-conversations-plan.md, Part 2):
routes fan out — every enabled matching route delivers, not just the first
(the guard's activity.* and a herald's activity.person.jamie both fire on a
recognized sighting) — and routes may carry valves (hours / cooldown_s /
budget_per_hour) enforced here. Valve semantics split by what the valve
MEANS (2026-09-14, the dropped-leave-clip lesson):

- ``hours`` blocks → 'throttled', never retried. Watching outside the
  approved window is a policy statement, not latency.
- ``cooldown_s`` / ``budget_per_hour`` block → DEFER: the event stays
  pending and rides the next eligible fire together with everything
  accumulated since. The rate valves bound how often the watch turns run,
  not how much the watch sees — a fire carries the whole backlog (capped:
  MAX_EVENTS_PER_STEER oldest-first, excess dropped with a note in the
  steer). TTL still bounds deferral (an event that outlives its ttl_s
  expires instead).

NULL valves (the seeded routes) behave exactly as before.

Failure semantics: the steer is created before rows are marked, so a crash
re-delivers a batch; dedup_key absorbs source-side retries and the steer's
own content makes re-delivery visible rather than confusing. Worst case is
late, never lost. With fan-out a partial failure (one target steered, one
not) re-delivers to both next tick — visible duplication, never loss.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from server.context import AppContext
from server.repositories.stimulus import StimulusRepository

logger = logging.getLogger(__name__)

# Procesed rows older than this are pruned (events are not a ledger; the
# delivered_steer pointer keeps the audit trail's meaning). Route fires ride
# the same cutoff — they're valve state, not history.
PRUNE_AFTER_DAYS = 30

# Route valve hours are local house time. One timezone for the box (frigate
# skill config and `bob events` default to the same).
_LOCAL_TZ = ZoneInfo(os.environ.get("BOB_TZ", "Australia/Perth"))

_HHMM_WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")

# Cap on how many deferred events one steer may carry (video reads per
# wake). Oldest-first; the excess is dropped with a note so the watch knows
# the storm was bigger than the wake.
MAX_EVENTS_PER_STEER = 5


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _route_matches(event: dict[str, Any], route: dict[str, Any]) -> bool:
    if route["source"] not in ("*", event["source"]):
        return False
    if not fnmatch.fnmatchcase(event["type"], route["type_pattern"]):
        return False
    return route["level"] in ("*", event["level"])


def match_route(event: dict[str, Any],
                routes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """First enabled route matching source/type/level, ordered by
    (priority, id) — sorted here so callers can pass routes in any order."""
    for r in sorted(routes, key=lambda r: (r.get("priority") or 0, r.get("id") or 0)):
        if _route_matches(event, r):
            return r
    return None


def match_routes(event: dict[str, Any],
                 routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every enabled route matching source/type/level — fan-out delivery.
    Ordered by (priority, id) so the first entry is the audit route_id."""
    ordered = sorted(routes, key=lambda r: (r.get("priority") or 0, r.get("id") or 0))
    return [r for r in ordered if _route_matches(event, r)]


def _in_hours(now_local: datetime, window: str | None) -> bool:
    """Is ``now_local`` inside an 'HH:MM-HH:MM' window? Windows may wrap
    midnight ('22:00-06:00'). Unparseable windows read as 24h with a warning —
    the request tool validates the format, so this is a manual-SQL escape."""
    m = _HHMM_WINDOW_RE.match((window or "").strip())
    if not m:
        if window:
            logger.warning("stimulus: unparseable hours %r — treated as 24h", window)
        return True
    start = int(m[1]) * 60 + int(m[2])
    end = int(m[3]) * 60 + int(m[4])
    cur = now_local.hour * 60 + now_local.minute
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


async def _utility_alive(ctx: AppContext, target: str) -> bool:
    """Master kill switch + per-conversation enabled. A dead utility target
    reads as log-only (plan Part 3): turning a behavior off never errors."""
    uc_settings = getattr(ctx.settings, "utility_conversations", None)
    if uc_settings is not None and not uc_settings.enabled:
        return False
    from server.repositories.utility_conversations import (
        UtilityConversationRepository,
    )
    row = await UtilityConversationRepository(ctx.db).get(target)
    return bool(row and row["enabled"])


async def _valve_block_reason(repo: StimulusRepository, route: dict[str, Any],
                              now: datetime, now_local: datetime,
                              stats_cache: dict[int, dict[str, Any]]) -> str | None:
    """None (deliver), 'hours' (drop — policy statement), or 'rate'
    (defer — cost valve; the event rides the next eligible fire)."""
    if not _in_hours(now_local, route.get("hours")):
        return "hours"
    cooldown_s = route.get("cooldown_s")
    budget = route.get("budget_per_hour")
    if not cooldown_s and not budget:
        return None
    stats = stats_cache.get(route["id"])
    if stats is None:
        stats = await repo.fire_stats(route["id"])
        stats_cache[route["id"]] = stats
    last = _parse_ts(stats.get("last_fire") or "")
    if cooldown_s and last and (now - last).total_seconds() < cooldown_s:
        return "rate"
    if budget and stats.get("fires_window", 0) >= budget:
        return "rate"
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
        return {"pending": 0, "expired": 0, "steered": 0, "logged": 0,
                "throttled": 0, "deferred": 0}

    now = datetime.now(timezone.utc)
    now_local = datetime.now(_LOCAL_TZ)
    routes = await repo.routes(enabled_only=True)

    utility_liveness: dict[str, bool] = {}
    stats_cache: dict[int, dict[str, Any]] = {}
    route_stamps: dict[int, int] = {}  # event id -> audit route_id (first match)

    expired_ids: list[int] = []
    log_only_ids: list[int] = []
    throttled_ids: list[int] = []
    deferred_ids: list[int] = []  # rate-valve blocked — left pending
    batches: dict[str, list[dict[str, Any]]] = {}
    batch_routes: dict[str, set[int]] = {}

    for e in pending:
        ttl = e.get("ttl_s")
        ts = _parse_ts(e.get("ts") or "")
        if ttl is not None and ts is not None and now > ts + timedelta(seconds=ttl):
            expired_ids.append(e["id"])
            continue
        if e["level"] != "action":
            log_only_ids.append(e["id"])  # info never wakes anyone
            continue
        matched = match_routes(e, routes)
        if not matched:
            log_only_ids.append(e["id"])
            continue
        route_stamps[e["id"]] = matched[0]["id"]

        delivering: list[str] = []
        block_reasons: set[str] = set()
        seen_targets: set[str] = set()  # one delivery per target per event,
        # even when several routes point at the same session (the first
        # delivering route wins; later duplicates skip their valve check)
        for r in matched:
            target = r.get("target_session")
            if not target or target in seen_targets:
                continue  # log-only route (NULL target), or already delivering
            if target not in utility_liveness and \
                    target.startswith("agent:") and target.endswith(":utility"):
                utility_liveness[target] = await _utility_alive(ctx, target)
            if target in utility_liveness and not utility_liveness[target]:
                logger.info("stimulus: utility target %s off/missing — "
                            "its route delivers nothing this pass", target)
                continue
            reason = await _valve_block_reason(repo, r, now, now_local, stats_cache)
            if reason:
                block_reasons.add(reason)
                continue
            delivering.append(target)
            seen_targets.add(target)
            batches.setdefault(target, []).append(e)
            batch_routes.setdefault(target, set()).add(r["id"])

        if not delivering:
            if "rate" in block_reasons:
                # Cost valve: keep it pending — it rides the next eligible
                # fire with everything else deferred since (TTL bounds the
                # wait; the enter-garden/leave-garden pair must land in one
                # watch, 2026-09-14).
                deferred_ids.append(e["id"])
            elif "hours" in block_reasons:
                throttled_ids.append(e["id"])  # out-of-window: policy drop
            else:  # NULL-target routes only, or utility targets all off
                log_only_ids.append(e["id"])

    await repo.mark_processed(expired_ids, "expired")
    await repo.mark_processed(log_only_ids, "log-only")
    await repo.mark_processed(throttled_ids, "throttled")
    for event_id, route_id in route_stamps.items():
        await repo.stamp_route_ids([event_id], route_id)

    steered = 0
    for target, events in sorted(batches.items()):
        from server.services.wake_service import wake_conversation
        # Backlog cap: a deferred pile rides one wake, oldest-first; the
        # excess is dropped (a storm bigger than the cap is its own signal,
        # and the note tells the watch it happened).
        overflow = events[MAX_EVENTS_PER_STEER:]
        events = events[:MAX_EVENTS_PER_STEER]
        instruction = render_instruction(events)
        if overflow:
            instruction += (
                f"\n[{len(overflow)} further event(s) held back to bound this "
                f"wake — the oldest {len(events)} above are the record]")
        try:
            armed = await wake_conversation(
                ctx, target, instruction,
                call_category="steer",
                metadata={"stimulus_ids": [e["id"] for e in events + overflow],
                          "stimulus_sources": sorted(
                              {e["source"] for e in events + overflow})},
                provenance="steer")
        except Exception:
            logger.exception("stimulus steer failed for %s (ids %s)",
                             target, [e["id"] for e in events])
            continue  # leave pending — retried next tick
        outcome = "steer:ok" if armed else "steer:undispatched"
        await repo.mark_processed([e["id"] for e in events], outcome)
        if overflow:
            await repo.mark_processed([e["id"] for e in overflow],
                                      "steer:overflow")
            logger.info("stimulus: %d overflow event(s) dropped from %s "
                        "backlog cap", len(overflow), target)
        await repo.record_fires(sorted(batch_routes[target]),
                                datetime.now(timezone.utc).isoformat())
        steered += len(events)
        logger.info("stimulus: steered %d event(s) -> %s (%s)",
                    len(events), target, outcome)

    # opportunistic prune (cheap; volume is tiny)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    try:
        await repo.prune_processed_before(cutoff)
        await repo.prune_fires_before(cutoff)
    except Exception:
        logger.exception("stimulus prune failed (non-fatal)")

    return {"pending": len(pending), "expired": len(expired_ids),
            "steered": steered, "logged": len(log_only_ids),
            "throttled": len(throttled_ids),
            "deferred": len(deferred_ids)}
