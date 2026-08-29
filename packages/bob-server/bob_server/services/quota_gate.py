"""Circuit breaker for provider credit/quota exhaustion.

When a provider returns a 429 ``insufficient_quota`` /
``credit_balance_exhausted`` error (OpenAI) or an insufficient-credits
error (OpenRouter), retrying immediately is pointless: no request will
succeed until the account is topped up. Without a gate, background tasks
(heartbeat email processing, attention probes, memory extraction) hammer
the API all night — observed at ~1,700 failed calls/hour during an outage.

The gate opens on the first quota error and fails every call fast (raising
``QuotaExhaustedError`` before any API request or llm_call_log row) for a
cooldown window. After the window expires calls flow again; if quota is
still exhausted the first real failure re-opens the gate. Any successful
call closes it. Net effect: at most one real probe attempt per cooldown
period.

State is keyed by provider ("openai" / "openrouter" — see
model_registry.provider_for): one provider running out of credit must not
fail-fast the other's traffic.

Module-level state, mirrors the executor-registry pattern in effects.py.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

COOLDOWN_S = 300.0

_open_until: dict[str, float] = {}
_trip_count: dict[str, int] = {}


class QuotaExhaustedError(RuntimeError):
    """Raised by check() while the gate is open (fail-fast)."""


def is_quota_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "insufficient_quota" in text or "credit_balance_exhausted" in text:
        return True
    # OpenRouter credit exhaustion phrasings.
    if "insufficient credits" in text or "not enough credits" in text:
        return True
    if "402" in text and "credit" in text:
        return True
    return False


def check(provider: str = "openai") -> None:
    """Raise QuotaExhaustedError if this provider's gate is open.

    Call before making a provider request.
    """
    remaining = _open_until.get(provider, 0.0) - time.monotonic()
    if remaining > 0:
        raise QuotaExhaustedError(
            f"{provider} quota exhausted (trip #{_trip_count.get(provider, 0)}); "
            f"failing fast for another {remaining:.0f}s — top up credits to resume"
        )


def record_failure(exc: BaseException, provider: str = "openai") -> bool:
    """Open this provider's gate if exc is a quota-exhaustion error. Returns True if opened."""
    if isinstance(exc, QuotaExhaustedError) or not is_quota_error(exc):
        return False
    _open_until[provider] = time.monotonic() + COOLDOWN_S
    _trip_count[provider] = _trip_count.get(provider, 0) + 1
    logger.warning(
        "quota gate OPEN for %s (trip #%d): provider credits exhausted; "
        "failing LLM calls fast for %.0fs",
        provider, _trip_count[provider], COOLDOWN_S,
    )
    return True


def record_success(provider: str = "openai") -> None:
    """Close this provider's gate after a successful call."""
    if _trip_count.get(provider, 0):
        logger.info("quota gate CLOSED for %s: provider call succeeded, quota recovered", provider)
    _open_until.pop(provider, None)
    _trip_count.pop(provider, None)


def status() -> dict:
    """Introspection for the dashboard: gate state without touching it.

    Flat ``open``/``remaining_s`` keys reflect the worst provider (the
    ops-status tile reads them); ``providers`` carries the breakdown.
    """
    providers: dict[str, dict] = {}
    any_open = False
    max_remaining = 0.0
    for provider in set(_open_until) | set(_trip_count):
        remaining = max(0.0, _open_until.get(provider, 0.0) - time.monotonic())
        open_ = remaining > 0
        any_open = any_open or open_
        max_remaining = max(max_remaining, remaining)
        providers[provider] = {
            "open": open_,
            "trip_count": _trip_count.get(provider, 0),
            "remaining_s": round(remaining),
        }
    return {
        "open": any_open,
        "remaining_s": round(max_remaining),
        "cooldown_s": COOLDOWN_S,
        "providers": providers,
    }


def reset() -> None:
    """Test helper: return every provider's gate to its initial state."""
    _open_until.clear()
    _trip_count.clear()
