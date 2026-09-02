"""Tests for quota-exhaustion handling in the WhatsApp bridge dispatch path.

Covers the two module-level helpers: error detection (string-based, since
openai_service wraps the SDK error in a RuntimeError) and the per-session
notification rate limit.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

from bob_server.services.whatsapp_bridge_service._service import (
    _QUOTA_NOTIFY_MIN_INTERVAL,
    _is_quota_error,
    _notify_quota_exhausted,
    _quota_notify_last,
)


def _reset_rate_limit_state():
    _quota_notify_last.clear()


def _seed_rate_limit(session_key: str, seconds_ago: float) -> None:
    _quota_notify_last[session_key] = time.monotonic() - seconds_ago


# ---------------------------------------------------------------------------
# _is_quota_error
# ---------------------------------------------------------------------------


def test_is_quota_error_detects_insufficient_quota():
    exc = RuntimeError(
        "OpenAI API error: Error code: 429 - {'error': {"
        "'message': 'You exceeded your current quota ...', "
        "'code': 'insufficient_quota'}}"
    )
    assert _is_quota_error(exc)


def test_is_quota_error_detects_429_with_quota_keyword():
    assert _is_quota_error(RuntimeError("Error code: 429 - billing quota issue"))


def test_is_quota_error_ignores_generic_429_without_quota():
    """A 429 from rate limiting (not billing) should not trigger the path."""
    assert not _is_quota_error(RuntimeError("Error code: 429 - rate limit hit"))


def test_is_quota_error_ignores_unrelated_errors():
    assert not _is_quota_error(RuntimeError("Connection reset by peer"))
    assert not _is_quota_error(ValueError("bad input"))
    assert not _is_quota_error(RuntimeError("OpenAI API error: 500 internal"))


# ---------------------------------------------------------------------------
# _notify_quota_exhausted: rate limiting
# ---------------------------------------------------------------------------


async def test_notify_sends_when_no_prior_record():
    _reset_rate_limit_state()
    wa = AsyncMock()
    await _notify_quota_exhausted(wa, "chat@g.us", "agent:main:test:1")
    wa.send_message.assert_awaited_once()
    args, _ = wa.send_message.call_args
    assert args[0] == "chat@g.us"
    assert "credit" in args[1].lower()


async def test_notify_suppressed_within_interval():
    _reset_rate_limit_state()
    wa = AsyncMock()
    _seed_rate_limit("agent:main:test:2", seconds_ago=60.0)  # 1 min ago
    await _notify_quota_exhausted(wa, "chat@g.us", "agent:main:test:2")
    wa.send_message.assert_not_awaited()


async def test_notify_resends_after_interval_elapses():
    _reset_rate_limit_state()
    wa = AsyncMock()
    _seed_rate_limit("agent:main:test:3", seconds_ago=_QUOTA_NOTIFY_MIN_INTERVAL + 1.0)
    await _notify_quota_exhausted(wa, "chat@g.us", "agent:main:test:3")
    wa.send_message.assert_awaited_once()


async def test_notify_rate_limit_is_per_session():
    _reset_rate_limit_state()
    wa = AsyncMock()
    # Session A was notified 1 minute ago — suppressed.
    _seed_rate_limit("agent:main:test:A", seconds_ago=60.0)
    # Session B never notified — should send.
    await _notify_quota_exhausted(wa, "chat-a@g.us", "agent:main:test:A")
    await _notify_quota_exhausted(wa, "chat-b@g.us", "agent:main:test:B")

    calls = [c.args[0] for c in wa.send_message.call_args_list]
    assert calls == ["chat-b@g.us"]


async def test_notify_send_failure_does_not_raise():
    """A broken send must not mask the original quota error."""
    _reset_rate_limit_state()
    wa = AsyncMock()
    wa.send_message.side_effect = RuntimeError("bridge disconnected")
    # Should not raise — the caller is already in an error path.
    await _notify_quota_exhausted(wa, "chat@g.us", "agent:main:test:4")
