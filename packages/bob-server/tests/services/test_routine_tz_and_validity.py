"""Tests for routines: timezone-aware scheduling and validity window filtering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bob_server.cron import next_cron_occurrence
from bob_server.services.routine_service import (
    RoutineService,
    _format_routine_now,
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
# _format_routine_now — local-time header injection
# ---------------------------------------------------------------------------


def test_format_routine_now_named_timezone_carries_iana_and_offset():
    row = {"timezone": "Australia/Perth"}
    header = _format_routine_now(row)
    assert header.startswith("[Routine local time: ")
    assert "Australia/Perth" in header
    # Perth is UTC+08:00 year-round (no DST)
    assert "UTC+0800" in header
    assert header.endswith("]")


def test_format_routine_now_falls_back_to_server_local_with_offset():
    row = {"timezone": None}
    header = _format_routine_now(row)
    assert "server local" in header
    # Offset always present so the model can convert to other zones
    assert "UTC+" in header or "UTC-" in header or "UTC+0000" in header


def test_format_routine_now_uses_routine_tz_not_ambient_utc():
    # 07:00 Perth is 23:00 UTC the prior day. The header must reflect Perth's
    # wall clock, not the model's ambient UTC sense — that divergence is the
    # whole reason the header exists.
    import datetime as _dt

    row = {"timezone": "Australia/Perth"}
    header = _format_routine_now(row)
    perth_now = _dt.datetime.now(ZoneInfo("Australia/Perth"))
    assert perth_now.strftime("%d %B %Y") in header
    assert perth_now.strftime("%H:%M") in header


# ---------------------------------------------------------------------------
# build_common_tools — routine tool gating
# ---------------------------------------------------------------------------


def test_build_common_tools_excludes_routine_tools_when_disabled(ctx):
    from bob_server.services.tool_registry import build_common_tools

    tools = build_common_tools(ctx, session_key="agent:main:test:x:x", include_routines=False)
    names = {t.name for t in tools}
    assert "read_routine" not in names
    assert "write_routine" not in names
    assert "delete_routine" not in names


def test_build_common_tools_includes_routine_tools_by_default(ctx):
    from bob_server.services.tool_registry import build_common_tools

    tools = build_common_tools(ctx, session_key="agent:main:test:x:x")
    names = {t.name for t in tools}
    assert "read_routine" in names
    assert "write_routine" in names


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
# Cross-timezone due-ness — the continuous-fire regression
# ---------------------------------------------------------------------------


async def test_cross_tz_future_routine_not_due_under_mismatched_offset(svc, session_key):
    # A Europe/Paris routine due ~2h in the future. Under the old bare
    # `next_run_at <= now` TEXT compare, the Paris ISO string (e.g. ...T18:30+02:00)
    # sorts lexicographically below a Perth server now (...T...+08:00) on the
    # same date and reads as "due" — firing every heartbeat. datetime() on both
    # sides normalizes to UTC so the real-time answer wins.
    paris = ZoneInfo("Europe/Paris")
    future_paris = (datetime.now(UTC) + timedelta(hours=2)).astimezone(paris)
    await svc.upsert_routine(
        session_key=session_key,
        name="paris-future",
        schedule="30 6 * * *",
        prompt="x",
        next_run_at=future_paris.isoformat(),
        timezone="Europe/Paris",
    )
    due = await svc.get_due_routines()
    assert "paris-future" not in [r["name"] for r in due]


async def test_cross_tz_past_routine_is_due_under_mismatched_offset(svc, session_key):
    # Inverse: a Paris routine whose slot genuinely passed must still be due.
    paris = ZoneInfo("Europe/Paris")
    past_paris = (datetime.now(UTC) - timedelta(hours=2)).astimezone(paris)
    await svc.upsert_routine(
        session_key=session_key,
        name="paris-past",
        schedule="30 6 * * *",
        prompt="x",
        next_run_at=past_paris.isoformat(),
        timezone="Europe/Paris",
    )
    due = await svc.get_due_routines()
    assert "paris-past" in [r["name"] for r in due]


async def test_claim_on_future_routine_returns_false_and_does_not_advance(svc, session_key):
    # claim() must refuse a future routine, otherwise the per-heartbeat loop
    # returns: claim "succeeds", rewrites next_run_at to the same future value,
    # and the next heartbeat fires it again.
    paris = ZoneInfo("Europe/Paris")
    future_paris = (datetime.now(UTC) + timedelta(hours=2)).astimezone(paris)
    routine = await svc.upsert_routine(
        session_key=session_key,
        name="paris-future-claim",
        schedule="30 6 * * *",
        prompt="x",
        next_run_at=future_paris.isoformat(),
        timezone="Europe/Paris",
    )
    won = await svc.claim(routine["id"], "2099-01-01T00:00:00+00:00")
    assert won is False
    refreshed = await svc.get_routine(session_key, "paris-future-claim")
    assert refreshed["next_run_at"] == future_paris.isoformat()  # unchanged


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
