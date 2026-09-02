"""E2E smoke test: verify device identity + websocket connect to OpenClaw gateway."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytestmark = pytest.mark.openclaw_live


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_gateway_agent_smoke(live_openclaw: Any) -> None:
    """Send a trivial agent prompt through the gateway and verify we get a response.

    This tests that:
    - Device identity is loaded/created
    - Ed25519 signature is accepted by the gateway
    - operator.write scope is granted
    - The agent method works end-to-end
    """
    session_key = live_openclaw.new_session_key("gateway-smoke")
    try:
        response = live_openclaw.run_agent(
            session_key=session_key,
            message="Say hello in one word.",
            thinking="off",
            timeout_seconds=30.0,
        )
    except Exception as exc:
        msg = str(exc).lower()
        if "rate limit" in msg or "429" in msg or "quota" in msg:
            pytest.skip(f"Model backend rate limited: {exc}")
        if "timed out" in msg:
            pytest.skip(f"Gateway timed out: {exc}")
        raise

    text = live_openclaw.response_text(response, session_key=session_key)
    assert text.strip(), f"Agent returned empty response: {response}"
