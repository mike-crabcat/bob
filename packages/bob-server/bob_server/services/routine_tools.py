"""Routine tools — read_routine, write_routine, delete_routine for LLM sessions."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import available_timezones

import yaml

from bob_server.cron import next_cron_occurrence, validate_cron_expression
from bob_server.services.tools import Tool, tool

if TYPE_CHECKING:
    from bob_server.context import AppContext

logger = logging.getLogger(__name__)


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
    return yaml.dump(payload, default_flow_style=False)


def make_routine_tools(
    ctx: AppContext,
    *,
    session_key: str,
) -> list[Tool]:
    from bob_server.services.routine_service import RoutineService

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

        next_at = next_cron_occurrence(schedule, timezone=timezone).isoformat()

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
        return _routine_to_yaml(routine)

    @tool
    async def delete_routine(name: str) -> str:
        """Delete a routine by name for this session."""
        deleted = await svc.delete_routine(session_key, name)
        if deleted:
            return json.dumps({"deleted": name})
        return json.dumps({"error": f"Routine '{name}' not found"})

    return [read_routine, write_routine, delete_routine]
