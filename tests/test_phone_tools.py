"""Tests for phone_tools.py — get_call_status (status query only).

The make_phone_call and place_realtime_call entry points were removed when
create_subagent(agent_type="openai_voice") became the canonical dispatch
surface for outbound calls. Active-call placement is tested via the
subagent service tests.
"""

from __future__ import annotations

import json

from server.config import PhoneSettings
from server.context import AppContext
from server.services.phone_tools import make_phone_tools


def _make_phone_settings(*, enabled: bool = True) -> PhoneSettings:
    return PhoneSettings(
        enabled=enabled,
        twilio_account_sid="ACtest",
        twilio_auth_token="test_token",
        twilio_phone_number="+61400000000",
        base_url="https://example.com",
    )


def _make_ctx(ctx: AppContext, *, phone_enabled: bool = True) -> AppContext:
    object.__setattr__(ctx.settings, "phone", _make_phone_settings(enabled=phone_enabled))
    return ctx


async def _insert_call(db, *, call_id: str = "test-call-id", status: str = "ringing", phone: str = "+61400111111"):
    await db.execute(
        """INSERT INTO phone_calls (id, call_sid, phone_number, direction, status, agenda, started_at)
           VALUES (?, 'CA_test', ?, 'outbound', ?, 'test', datetime('now'))""",
        (call_id, phone, status),
    )


def _get_handler(tools, name):
    for t in tools:
        if t.name == name:
            return t.handler
    raise KeyError(f"Tool {name} not found")


async def test_get_call_status_found(ctx: AppContext):
    ctx = _make_ctx(ctx)
    await _insert_call(ctx.db, call_id="status-test-id", status="active", phone="+61400111111")
    tools = make_phone_tools(ctx)
    handler = _get_handler(tools, "get_call_status")

    result = json.loads(await handler(call_id="status-test-id"))
    assert result["ok"] is True
    assert result["status"] == "active"
    assert result["phone_number"] == "+61400111111"


async def test_get_call_status_not_found(ctx: AppContext):
    ctx = _make_ctx(ctx)
    tools = make_phone_tools(ctx)
    handler = _get_handler(tools, "get_call_status")

    result = json.loads(await handler(call_id="nonexistent"))
    assert result["ok"] is False
    assert "not found" in result["error"]


async def test_make_phone_tools_returns_expected_tools(ctx: AppContext):
    ctx = _make_ctx(ctx)
    tools = make_phone_tools(ctx)
    names = {t.name for t in tools}
    assert names == {"get_call_status"}
