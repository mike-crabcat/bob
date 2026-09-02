"""Tests for the provider quota circuit breaker (services/quota_gate.py)."""

import pytest

from server.services import quota_gate


@pytest.fixture(autouse=True)
def _reset_gate():
    quota_gate.reset()
    yield
    quota_gate.reset()


def test_gate_closed_by_default():
    quota_gate.check()  # must not raise


def test_quota_error_detection():
    assert quota_gate.is_quota_error(Exception(
        "Error code: 429 - {'error': {'message': 'You have no credits remaining.', "
        "'type': 'insufficient_quota', 'code': 'credit_balance_exhausted'}}"
    ))
    assert not quota_gate.is_quota_error(Exception("Error code: 429 - rate limit, slow down"))
    assert not quota_gate.is_quota_error(Exception("connection reset"))


def test_quota_failure_opens_gate():
    opened = quota_gate.record_failure(Exception("429 insufficient_quota"))
    assert opened
    with pytest.raises(quota_gate.QuotaExhaustedError):
        quota_gate.check()


def test_non_quota_failure_does_not_open():
    assert not quota_gate.record_failure(Exception("timeout"))
    quota_gate.check()


def test_success_closes_gate():
    quota_gate.record_failure(Exception("credit_balance_exhausted"))
    quota_gate.record_success()
    quota_gate.check()


def test_gate_reopens_after_cooldown_expiry(monkeypatch):
    quota_gate.record_failure(Exception("insufficient_quota"))
    # Jump past the cooldown window: calls flow again (single probe attempt)
    monkeypatch.setattr(
        quota_gate.time, "monotonic",
        lambda base=quota_gate.time.monotonic(): base + quota_gate.COOLDOWN_S + 1,
    )
    quota_gate.check()  # must not raise
    # Probe still failing on quota -> gate re-opens
    assert quota_gate.record_failure(Exception("insufficient_quota"))


def test_own_fail_fast_error_does_not_extend_cooldown():
    quota_gate.record_failure(Exception("insufficient_quota"))
    try:
        quota_gate.check()
    except quota_gate.QuotaExhaustedError as exc:
        # The fail-fast error itself must not re-trip the gate
        assert not quota_gate.record_failure(exc)
