"""Circuit breaker for provider credit/quota exhaustion.

When OpenAI returns a 429 ``insufficient_quota`` / ``credit_balance_exhausted``
error, retrying immediately is pointless: no request will succeed until the
account is topped up. Without a gate, background tasks (heartbeat email
processing, attention probes, memory extraction) hammer the API all night —
observed at ~1,700 failed calls/hour during an outage.

The gate opens on the first quota error and fails every call fast (raising
``QuotaExhaustedError`` before any API request or llm_call_log row) for a
cooldown window. After the window expires calls flow again; if quota is still
exhausted the first real failure re-opens the gate. Any successful call
closes it. Net effect: at most one real probe attempt per cooldown period.

Module-level state, mirrors the executor-registry pattern in effects.py.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

COOLDOWN_S = 300.0

_open_until: float = 0.0
_trip_count: int = 0


class QuotaExhaustedError(RuntimeError):
    """Raised by check() while the quota gate is open (fail-fast)."""


def is_quota_error(exc: BaseException) -> bool:
    text = str(exc)
    return "insufficient_quota" in text or "credit_balance_exhausted" in text


def check() -> None:
    """Raise QuotaExhaustedError if the gate is open.

    Call before making any provider request.
    """
    remaining = _open_until - time.monotonic()
    if remaining > 0:
        raise QuotaExhaustedError(
            f"provider quota exhausted (trip #{_trip_count}); "
            f"failing fast for another {remaining:.0f}s — top up credits to resume"
        )


def record_failure(exc: BaseException) -> bool:
    """Open the gate if exc is a quota-exhaustion error. Returns True if opened."""
    global _open_until, _trip_count
    if isinstance(exc, QuotaExhaustedError) or not is_quota_error(exc):
        return False
    _open_until = time.monotonic() + COOLDOWN_S
    _trip_count += 1
    logger.warning(
        "quota gate OPEN (trip #%d): provider credits exhausted; "
        "failing LLM calls fast for %.0fs",
        _trip_count, COOLDOWN_S,
    )
    return True


def record_success() -> None:
    """Close the gate after any successful provider call."""
    global _open_until, _trip_count
    if _trip_count:
        logger.info("quota gate CLOSED: provider call succeeded, quota recovered")
    _open_until = 0.0
    _trip_count = 0


def reset() -> None:
    """Test helper: return the gate to its initial state."""
    global _open_until, _trip_count
    _open_until = 0.0
    _trip_count = 0
