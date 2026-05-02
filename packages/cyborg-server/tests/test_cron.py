"""Tests for the shared cron module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cyborg_server.cron import (
    next_cron_occurrence,
    validate_cron_expression,
)


# ---------------------------------------------------------------------------
# validate_cron_expression
# ---------------------------------------------------------------------------


def test_validate_every_minute():
    assert validate_cron_expression("* * * * *") == "* * * * *"


def test_validate_specific_time():
    assert validate_cron_expression("30 14 * * *") == "30 14 * * *"


def test_validate_step():
    assert validate_cron_expression("*/5 * * * *") == "*/5 * * * *"


def test_validate_range():
    assert validate_cron_expression("0 9-17 * * *") == "0 9-17 * * *"


def test_validate_list():
    assert validate_cron_expression("0 0 1,15 * *") == "0 0 1,15 * *"


def test_validate_wrong_field_count():
    with pytest.raises(ValueError, match="five fields"):
        validate_cron_expression("* * *")


def test_validate_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        validate_cron_expression("60 * * * *")


def test_validate_invalid_token():
    with pytest.raises(ValueError, match="Unsupported"):
        validate_cron_expression("abc * * * *")


# ---------------------------------------------------------------------------
# next_cron_occurrence
# ---------------------------------------------------------------------------


def test_next_cron_every_minute():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    result = next_cron_occurrence("* * * * *", start=start)
    assert result == start + timedelta(minutes=1)


def test_next_cron_specific_hour():
    start = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
    result = next_cron_occurrence("0 14 * * *", start=start)
    assert result.hour == 14
    assert result.minute == 0


def test_next_cron_step():
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    result = next_cron_occurrence("*/15 * * * *", start=start)
    assert result.minute == 15
