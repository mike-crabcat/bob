"""Tests for routines: timezone-aware scheduling and validity window filtering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bob_server.cron import next_cron_occurrence
from bob_server.services.routine_service import (
    RoutineService,
    _outside_validity_window,
)


# ---------------------------------------------------------------------------
# next_cron_occurrence — timezone
# ---------------------------------------------------------------------------


def test_next_cron_with_timezone_is_tz_aware():
    result = next_cron_occurrence("0 7 * * *", timezone="Europe/Paris")
    assert result.tzinfo is not None
    assert result.utcoffset() is not None


def test_next_cron_with_timezone_interprets_wall_clock_in_zone():
    # 07:00 wall-clock Paris is 05:00 UTC during CEST (summer, +02:00)
    start = datetime(2026, 7, 1, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
    result = next_cron_occurrence("0 7 * * *", start=start, timezone="Europe/Paris")
    assert result.hour == 7
    assert result.tzname() == "CEST"
    assert result.astimezone(UTC).hour == 5


def test_next_cron_with_timezone_dst_winter_offset():
    # 07:00 wall-clock Paris is 06:00 UTC during CET (winter, +01:00)
    start = datetime(2026, 1, 15, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
    result = next_cron_occurrence("0 7 * * *", start=start, timezone="Europe/Paris")
    assert result.hour == 7
    assert result.tzname() == "CET"
    assert result.astimezone(UTC).hour == 6


def test_next_cron_without_timezone_matches_legacy_behavior():
    start = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    legacy = next_cron_occurrence("0 14 * * *", start=start)
    # Calling with timezone=None should produce the same wall-clock answer.
    explicit = next_cron_occurrence("0 14 * * *", start=start, timezone=None)
    assert legacy == explicit


# ---------------------------------------------------------------------------
# _outside_validity_window — pure helper
# ---------------------------------------------------------------------------


def test_validity_window_open_when_both_null():
    row = {"timezone": None, "valid_from": None, "valid_until": None}
    assert _outside_validity_window(row) is False


def test_validity_window_excludes_before_valid_from():
    future = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    row = {"timezone": "UTC", "valid_from": future, "valid_until": None}
    assert _outside_validity_window(row) is True


def test_validity_window_includes_after_valid_from():
    past = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
    row = {"timezone": "UTC", "valid_from": past, "valid_until": None}
    assert _outside_validity_window(row) is False


def test_validity_window_excludes_after_valid_until_date_only():
    # Date-only upper bound: routine still fires through end of that day in tz.
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    row = {"timezone": "UTC", "valid_from": None, "valid_until": yesterday}
    assert _outside_validity_window(row) is True


def test_validity_window_includes_on_valid_until_date():
    today = datetime.now(UTC).date().isoformat()
    row = {"timezone": "UTC", "valid_from": None, "valid_until": today}
    assert _outside_validity_window(row) is False


def test_validity_window_tz_aware_comparison():
    # A valid_until that is still in the future in Sydney but past in UTC
    # should still keep the routine alive when the routine's tz is Sydney.
    now_utc = datetime.now(UTC)
    sydney = ZoneInfo("Australia/Sydney")
    # 10 hours ahead — a bound 5 hours in the future (UTC) is 15 hours ahead in Sydney's yesterday.
    bound = (now_utc + timedelta(hours=5)).isoformat()
    row = {"timezone": "Australia/Sydney", "valid_from": None, "valid_until": bound}
    # In Sydney, now + 5h UTC equals now + 15h Sydney, so the bound is still in the future.
    assert _outside_validity_window(row) is False


def test_validity_window_malformed_bound_treated_as_open():
    row = {"timezone": "UTC", "valid_from": "not-a-date", "valid_until": None}
    assert _outside_validity_window(row) is False


# ---------------------------------------------------------------------------
# RoutineService — DB integration
# ---------------------------------------------------------------------------


@pytest.fixture
async def svc(ctx):
    return RoutineService(ctx)


@pytest.fixture
def session_key():
    return "agent:main:test:group:test"


async def test_upsert_routine_stores_tz_and_validity(svc, session_key):
    routine = await svc.upsert_routine(
        session_key=session_key,
        name="paris-brief",
        schedule="0 7 * * *",
        prompt="Send brief.",
        next_run_at="2026-06-23T07:00:00+02:00",
        timezone="Europe/Paris",
        valid_from="2026-06-23",
        valid_until="2026-07-15",
    )
    assert routine["timezone"] == "Europe/Paris"
    assert routine["valid_from"] == "2026-06-23"
    assert routine["valid_until"] == "2026-07-15"


async def test_upsert_routine_defaults_null_tz_and_window(svc, session_key):
    routine = await svc.upsert_routine(
        session_key=session_key,
        name="legacy-routine",
        schedule="0 7 * * *",
        prompt="Send brief.",
        next_run_at="2026-06-23T07:00:00+00:00",
    )
    assert routine["timezone"] is None
    assert routine["valid_from"] is None
    assert routine["valid_until"] is None


async def test_get_due_routines_filters_outside_validity_window(svc, session_key):
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    past = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    await svc.upsert_routine(
        session_key=session_key,
        name="expired",
        schedule="* * * * *",
        prompt="Should not fire.",
        next_run_at=past,
        timezone="UTC",
        valid_until=yesterday,
    )
    due = await svc.get_due_routines()
    names = [r["name"] for r in due]
    assert "expired" not in names


async def test_get_due_routines_includes_inside_validity_window(svc, session_key):
    past = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    today = datetime.now(UTC).date().isoformat()
    future = (datetime.now(UTC) + timedelta(days=10)).date().isoformat()
    await svc.upsert_routine(
        session_key=session_key,
        name="active",
        schedule="* * * * *",
        prompt="Should fire.",
        next_run_at=past,
        timezone="UTC",
        valid_from=today,
        valid_until=future,
    )
    due = await svc.get_due_routines()
    names = [r["name"] for r in due]
    assert "active" in names


# ---------------------------------------------------------------------------
# write_routine tool — input validation
# ---------------------------------------------------------------------------


async def test_write_routine_tool_rejects_unknown_timezone(svc, session_key):
    from bob_server.services.routine_tools import make_routine_tools

    ctx = svc.ctx
    tools = make_routine_tools(ctx, session_key=session_key)
    write_tool = next(t for t in tools if t.name == "write_routine")

    result = await write_tool.handler(
        routine_yaml=(
            "name: bad-tz\n"
            "schedule: '0 7 * * *'\n"
            "prompt: test\n"
            "timezone: Foo/Bar\n"
        )
    )
    assert "Unknown timezone" in result


async def test_write_routine_tool_rejects_bad_valid_until(svc, session_key):
    from bob_server.services.routine_tools import make_routine_tools

    ctx = svc.ctx
    tools = make_routine_tools(ctx, session_key=session_key)
    write_tool = next(t for t in tools if t.name == "write_routine")

    result = await write_tool.handler(
        routine_yaml=(
            "name: bad-bound\n"
            "schedule: '0 7 * * *'\n"
            "prompt: test\n"
            "valid_until: not-a-date\n"
        )
    )
    assert "Invalid valid_until" in result


async def test_write_routine_tool_accepts_tz_and_validity(svc, session_key):
    from bob_server.services.routine_tools import make_routine_tools

    ctx = svc.ctx
    tools = make_routine_tools(ctx, session_key=session_key)
    write_tool = next(t for t in tools if t.name == "write_routine")

    result = await write_tool.handler(
        routine_yaml=(
            "name: paris-morning\n"
            "schedule: '0 7 * * *'\n"
            "prompt: Send brief.\n"
            "timezone: Europe/Paris\n"
            "valid_from: '2026-06-23'\n"
            "valid_until: '2026-07-15'\n"
        )
    )
    assert "Europe/Paris" in result
    assert "2026-06-23" in result
    assert "2026-07-15" in result
