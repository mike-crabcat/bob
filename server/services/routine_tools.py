"""Routine tools — read_routine, write_routine, delete_routine for LLM sessions."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import available_timezones

import yaml

from server.cron import next_cron_occurrence, validate_cron_expression
from server.services.tools import Tool, tool

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)

# Routine dispatch withholds routine-management tools (see tool_registry), so a
# prompt that asks its own runs to create/modify/delete routines can never be
# obeyed — the run just burns tokens hunting for the tools and may narrate the
# failure to the channel (seen in production 2026-08-17). Expiry belongs in
# valid_until, not in the prompt.
_ROUTINE_SELF_MANAGEMENT_RE = re.compile(
    r"\b(?:read|write|delete|remove|creat\w*|updat\w*|modif\w*|disabl\w*|enabl\w*)"
    r"\s+(?:this|the|a|any)\s+routine"
    r"|\b(?:read|write|delete)_routine\b",
    re.IGNORECASE,
)


def _next_fire_local(routine: dict) -> str:
    """Next fire as a human wall-clock in the routine's own timezone — the
    number to sanity-check against intent. Live 2026-09-01: three crypto
    routines were written with UTC cron hours against the documented
    local-hours contract and fired at 1am/4am; the model only ever saw the
    raw UTC next_run_at and never noticed."""
    raw = routine.get("next_run_at")
    if not raw:
        return ""
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return ""
    tz = None
    if routine.get("timezone"):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(routine["timezone"])
        except Exception:
            tz = None
    if tz is None:
        tz = datetime.now().astimezone().tzinfo
    return when.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


_GROUP_ID_RE = re.compile(r"\b(\d{15,20})(?:@g\.us)?\b")


def _target_session_mismatch(session_key: str, prompt: str) -> str | None:
    """A group id named in the prompt that differs from the binding session.

    write_routine binds to the CREATING session, so a prompt that targets a
    different group ("post the report to group-crypto-bob (120363…)") makes
    every fire run in the wrong conversation: routine replies land there, and
    the report body rides whatever send tool the run picks — the 8 Sep 2026
    crypto morning report landed in Pirate Radio this way (routines were born
    in a radio-group chat). 15-20-digit numbers are WhatsApp group ids for
    all practical purposes (phones top out ~13 digits)."""
    if ":whatsapp:group:" in session_key:
        own = session_key.rsplit(":", 1)[-1]
    else:
        own = ""
    for match in _GROUP_ID_RE.finditer(prompt or ""):
        gid = match.group(1)
        if gid != own:
            return gid + ("@g.us" if "@g.us" not in (prompt or "") else "")
    return None


def _routine_to_yaml(routine: dict) -> str:
    payload: dict = {
        "name": routine["name"],
        "schedule": routine["schedule"],
        "prompt": routine["prompt"],
        "enabled": bool(routine["enabled"]),
    }
    if routine.get("timezone"):
        payload["timezone"] = routine["timezone"]
    if routine.get("valid_from"):
        payload["valid_from"] = routine["valid_from"]
    if routine.get("valid_until"):
        payload["valid_until"] = routine["valid_until"]
    local = _next_fire_local(routine)
    if local:
        payload["next_fire_local"] = local
    return yaml.dump(payload, default_flow_style=False)


def make_routine_tools(
    ctx: AppContext,
    *,
    session_key: str,
) -> list[Tool]:
    from server.services.routine_service import RoutineService

    svc = RoutineService(ctx)

    @tool
    async def read_routine(name: str = "") -> str:
        """Read a routine by name, or list all routines for this session if name is omitted.
        Returns YAML for a single routine, or a JSON list of routine summaries."""
        if name.strip():
            routine = await svc.get_routine(session_key, name.strip())
            if not routine:
                return json.dumps({"error": f"Routine '{name}' not found"})
            return _routine_to_yaml(routine)

        routines = await svc.list_routines(session_key)
        if not routines:
            return "No routines configured for this session."

        summaries = [
            {
                "name": r["name"],
                "schedule": r["schedule"],
                "enabled": bool(r["enabled"]),
                "timezone": r.get("timezone"),
                "valid_from": r.get("valid_from"),
                "valid_until": r.get("valid_until"),
            }
            for r in routines
        ]
        return json.dumps({"routines": summaries})

    @tool
    async def write_routine(routine_yaml: str) -> str:
        """Create or update a routine for this session. Accepts YAML with fields: name, schedule (cron), prompt, enabled.
        The prompt must contain ONLY the action to perform — never include schedule/timing language
        (e.g. "At 9am each day", "Every Monday"). The schedule is handled by the separate schedule field.

        The prompt is an operational instruction addressed to the future run, not the user's
        request restated: rewrite it as concrete steps that run can perform on its own. Do not
        include instructions to create, modify, or delete routines (routine runs cannot use
        those tools — put expiry in valid_until instead), and do not bake in volatile facts the
        run should re-check at execution time (e.g. who has currently replied, current status) —
        describe how to check them (e.g. read the recent session messages) instead.

        Optional fields:
          timezone: IANA name (e.g. "Europe/Paris"). Defaults to the server's local timezone.
                    The cron wall-clock fields are interpreted in this zone.
          valid_from: ISO date or datetime (e.g. "2026-06-23" or "2026-06-23T07:00:00").
                      Routine will not fire before this time. Inclusive.
          valid_until: ISO date or datetime. Date-only bounds include the entire day in
                       the routine's timezone. Inclusive.

        Example:
          name: morning-digest
          schedule: "0 8 * * 1-5"
          prompt: Gather tech news and summarize.
          enabled: true
          timezone: Europe/Paris
          valid_from: "2026-06-23"
          valid_until: "2026-07-15\""""
        try:
            parsed = yaml.safe_load(routine_yaml)
        except yaml.YAMLError as e:
            return json.dumps({"error": f"Invalid YAML: {e}"})

        if not isinstance(parsed, dict) or "name" not in parsed:
            return json.dumps({"error": "YAML must include a 'name' field"})

        name = parsed["name"]
        schedule = parsed.get("schedule", "")
        prompt = parsed.get("prompt", "")
        enabled = parsed.get("enabled", True)
        timezone = parsed.get("timezone") or None
        valid_from = parsed.get("valid_from") or None
        valid_until = parsed.get("valid_until") or None

        if not schedule:
            return json.dumps({"error": "Routine must include a 'schedule' field"})
        if not prompt:
            return json.dumps({"error": "Routine must include a 'prompt' field"})
        if _ROUTINE_SELF_MANAGEMENT_RE.search(str(prompt)):
            return json.dumps({
                "error": (
                    "Routine prompts must not create, modify, or delete routines: routine runs "
                    "cannot use routine-management tools, so the instruction can never be obeyed. "
                    "Put expiry in valid_until, keep the prompt operational, and re-checkable "
                    "facts (like who has replied) should be looked up at run time rather than "
                    "stated in the prompt."
                ),
            })

        if timezone is not None:
            if timezone not in available_timezones():
                return json.dumps({"error": f"Unknown timezone: {timezone}"})

        for label, value in (("valid_from", valid_from), ("valid_until", valid_until)):
            if value is not None:
                try:
                    datetime.fromisoformat(str(value))
                except ValueError:
                    return json.dumps({"error": f"Invalid {label}: {value!r}"})

        try:
            validate_cron_expression(schedule)
        except ValueError as e:
            return json.dumps({"error": f"Invalid cron expression: {e}"})

        next_at = next_cron_occurrence(schedule, timezone=timezone).astimezone(UTC).isoformat()

        routine = await svc.upsert_routine(
            session_key=session_key,
            name=name,
            schedule=schedule,
            prompt=prompt,
            enabled=enabled,
            next_run_at=next_at,
            timezone=timezone,
            valid_from=valid_from,
            valid_until=valid_until,
        )
        out = _routine_to_yaml(routine)
        mismatch = _target_session_mismatch(session_key, str(prompt))
        if mismatch:
            out += (
                "\n# WARNING: this routine is BOUND TO THE SESSION THAT CREATED IT, but "
                f"its prompt targets group {mismatch}. Every run fires here "
                f"({session_key}): routine replies land in THIS chat, and any default "
                "send posts here too — the 2026-09-08 crypto report went to the wrong "
                "group this way. If the target group is where it belongs, create the "
                "routine in THAT conversation; if you keep it here, the prompt must "
                "name an explicit cross-session send every single run.")
        return out

    @tool
    async def delete_routine(name: str) -> str:
        """Delete a routine by name for this session."""
        deleted = await svc.delete_routine(session_key, name)
        if deleted:
            return json.dumps({"deleted": name})
        return json.dumps({"error": f"Routine '{name}' not found"})

    return [read_routine, write_routine, delete_routine]
