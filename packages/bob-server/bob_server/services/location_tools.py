"""Location tools for LLM function calling.

Exposes ``current_location()`` so Bob can answer "where am I" / "what's near
me" type questions by querying Home Assistant on demand. Only registered when
``ctx.settings.homeassistant.enabled``.

Usage:
    if ctx.settings.homeassistant.enabled:
        tools.extend(make_location_tools(ctx))
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from bob_server.context import AppContext
from bob_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)


def _get_ha_client(ctx: AppContext):
    """Lazily create and cache a HomeAssistantClient on the AppContext.

    Stored on ``ctx._ha_client`` (created on first use). Cached so repeated
    ``current_location()`` calls within a session reuse the same HTTP
    connection pool and the same in-memory state cache.
    """
    cached = getattr(ctx, "_ha_client", None)
    if cached is not None:
        return cached
    from bob_server.services.homeassistant_client import HomeAssistantClient

    settings = ctx.settings.homeassistant
    client = HomeAssistantClient(settings.url, settings.bearer_token)
    ctx._ha_client = client  # type: ignore[attr-defined]
    return client


def _format_location(payload: dict) -> str:
    """Render HA ``/api/states/<device_tracker>`` JSON as a human-readable line."""
    state = payload.get("state", "unknown")
    attrs = payload.get("attributes", {}) or {}
    lat = attrs.get("latitude")
    lon = attrs.get("longitude")
    accuracy = attrs.get("gps_accuracy")
    battery = attrs.get("battery_level")
    last_updated = payload.get("last_updated") or payload.get("last_changed")

    parts: list[str] = [f"zone={state}"]
    if lat is not None and lon is not None:
        coord = f"lat {lat:.5f}, lon {lon:.5f}"
        if accuracy is not None:
            coord += f" (±{int(accuracy)}m)"
        parts.append(coord)
    if battery is not None:
        parts.append(f"battery {int(battery)}%")
    if last_updated:
        try:
            ts = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            mins_ago = int((datetime.now(timezone.utc) - ts).total_seconds() // 60)
            parts.append(f"last_updated {mins_ago} min ago ({last_updated})")
        except (ValueError, TypeError):
            parts.append(f"last_updated {last_updated}")

    return "Currently: " + ", ".join(parts) + "."


def make_location_tools(ctx: AppContext) -> list[Tool]:
    """Create the location tool set bound to the given context."""

    settings = ctx.settings.homeassistant

    @tool
    async def current_location() -> str:
        """Return the user's current location from Home Assistant.

        Output includes zone state (e.g. 'home', 'not_home', or a custom zone
        name like 'Chamonix'), lat/lon with GPS accuracy, battery level, and
        last-updated timestamp. Cached for 2 minutes.

        Call this BEFORE answering location-dependent questions: 'where am I',
        'what's near me', 'find lunch nearby', 'how far to the hotel',
        'what should we do this afternoon'. Do not guess location from chat
        context — always call this tool.
        """
        client = _get_ha_client(ctx)
        try:
            payload = await client.get_state(settings.device_tracker_entity_id)
        except Exception as exc:
            logger.warning("HA get_state failed: %s", exc)
            return f"Location unavailable: Home Assistant query failed ({exc})."
        if payload is None:
            return (
                f"Location unavailable: HA entity "
                f"'{settings.device_tracker_entity_id}' not found."
            )
        return _format_location(payload)

    @tool
    async def location_history(hours: float = 24.0) -> str:
        """Return accumulated location pings from the location_history table.

        Pings are recorded every 15 minutes by the scheduled LocationFetchTask.
        Useful for answering 'what was my day like', 'where did we go
        yesterday', 'when did we arrive at X', 'how long did we stay at X'.
        Default last 24 hours; reduce hours for tighter windows.

        Each line shows: timestamp, zone, lat/lon, GPS accuracy, battery,
        and a stale marker if HA hadn't received fresh data from the phone
        at that time.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = await ctx.db.fetch_all(
            "SELECT fetched_at, latitude, longitude, gps_accuracy, zone_state, "
            "battery_level, ha_last_updated FROM location_history "
            "WHERE fetched_at >= ? ORDER BY fetched_at ASC",
            (cutoff,),
        )
        if not rows:
            return f"No location history in the last {hours}h."
        lines = [f"Location history ({len(rows)} ping(s), last {hours}h):"]
        for r in rows:
            lat = r["latitude"]
            lon = r["longitude"]
            acc = r["gps_accuracy"]
            acc_str = f"±{int(acc)}m" if acc is not None else "±?m"
            bat = r["battery_level"]
            bat_str = f"bat {int(bat)}%" if bat is not None else ""
            # 'stale' = HA hadn't received fresh data from phone at fetch time
            stale = ""
            fetched = r["fetched_at"]
            ha_ts = r["ha_last_updated"]
            if ha_ts and fetched:
                try:
                    f_dt = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
                    h_dt = datetime.fromisoformat(ha_ts.replace("Z", "+00:00"))
                    if (f_dt - h_dt).total_seconds() > 600:
                        stale = " (stale)"
                except (ValueError, TypeError):
                    pass
            lines.append(
                f"  {fetched[:19]} zone={r['zone_state'] or '?':12} "
                f"lat {lat:.4f} lon {lon:.4f} {acc_str} {bat_str}{stale}".rstrip()
            )
        return "\n".join(lines)

    return [current_location, location_history]
