"""Tests for phone_tools.py — make_phone_call and get_call_status."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cyborg_server.config import PhoneSettings
from cyborg_server.context import AppContext
from cyborg_server.routers.phone import _call_agendas
from cyborg_server.services.phone_tools import make_phone_tools


@contextmanager
def _mock_twilio():
    """Inject a fake twilio.rest module so lazy imports resolve without the real package."""
    mock_twilio = MagicMock()
    mock_twilio_rest = MagicMock()
    with patch.dict(sys.modules, {"twilio": mock_twilio, "twilio.rest": mock_twilio_rest}):
        yield mock_twilio_rest.Client


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


async def _insert_contact(db, *, contact_id: str = "c1", name: str = "Alice", phone: str = "+61400111111"):
    await db.execute(
        """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
           VALUES (?, ?, ?, 1, datetime('now'), datetime('now'))""",
        (contact_id, name, phone),
    )


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


# ---------------------------------------------------------------------------
# make_phone_call
# ---------------------------------------------------------------------------


async def test_make_phone_call_by_number(ctx: AppContext):
    ctx = _make_ctx(ctx)
    tools = make_phone_tools(ctx)
    handler = _get_handler(tools, "make_phone_call")

    mock_call = MagicMock()
    mock_call.sid = "CA_tool_call"
    mock_call.status = "ringing"

    with _mock_twilio() as MockClient:
        MockClient.return_value.calls.create.return_value = mock_call

        result = json.loads(await handler(agenda="Test call", phone_number="+61400222333"))

    assert result["ok"] is True
    assert result["call_sid"] == "CA_tool_call"
    assert result["phone_number"] == "+61400222333"
    assert result["status"] == "ringing"

    # Cleanup
    _call_agendas.pop("CA_tool_call", None)


async def test_make_phone_call_by_contact_id(ctx: AppContext):
    ctx = _make_ctx(ctx)
    await _insert_contact(ctx.db, contact_id="c1", name="Alice", phone="+61400111111")
    tools = make_phone_tools(ctx)
    handler = _get_handler(tools, "make_phone_call")

    mock_call = MagicMock()
    mock_call.sid = "CA_contact_call"
    mock_call.status = "ringing"

    with _mock_twilio() as MockClient:
        MockClient.return_value.calls.create.return_value = mock_call

        result = json.loads(await handler(agenda="Call Alice", contact_id="c1"))

    assert result["ok"] is True
    assert result["phone_number"] == "+61400111111"

    # Cleanup
    _call_agendas.pop("CA_contact_call", None)


async def test_make_phone_call_contact_not_found(ctx: AppContext):
    ctx = _make_ctx(ctx)
    tools = make_phone_tools(ctx)
    handler = _get_handler(tools, "make_phone_call")

    result = json.loads(await handler(agenda="Test", contact_id="nonexistent"))
    assert result["ok"] is False
    assert "not found" in result["error"]


async def test_make_phone_call_contact_no_phone(ctx: AppContext):
    ctx = _make_ctx(ctx)
    await ctx.db.execute(
        """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
           VALUES ('c2', 'Bob', '', 1, datetime('now'), datetime('now'))""",
    )
    tools = make_phone_tools(ctx)
    handler = _get_handler(tools, "make_phone_call")

    result = json.loads(await handler(agenda="Test", contact_id="c2"))
    assert result["ok"] is False
    assert "no phone number" in result["error"].lower()


async def test_make_phone_call_active_call_exists(ctx: AppContext):
    ctx = _make_ctx(ctx)
    await _insert_call(ctx.db, call_id="existing-call", status="ringing", phone="+61400111111")
    tools = make_phone_tools(ctx)
    handler = _get_handler(tools, "make_phone_call")

    result = json.loads(await handler(agenda="Test", phone_number="+61400111111"))
    assert result["ok"] is False
    assert "already in progress" in result["error"]


async def test_make_phone_call_phone_disabled(ctx: AppContext):
    ctx = _make_ctx(ctx, phone_enabled=False)
    tools = make_phone_tools(ctx)
    handler = _get_handler(tools, "make_phone_call")

    result = json.loads(await handler(agenda="Test", phone_number="+61400111111"))
    assert result["ok"] is False
    assert "not enabled" in result["error"]


async def test_make_phone_call_neither_id_nor_number(ctx: AppContext):
    ctx = _make_ctx(ctx)
    tools = make_phone_tools(ctx)
    handler = _get_handler(tools, "make_phone_call")

    result = json.loads(await handler(agenda="Test"))
    assert result["ok"] is False
    assert "contact_id" in result["error"] or "phone_number" in result["error"]


# ---------------------------------------------------------------------------
# get_call_status
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# tool registration
# ---------------------------------------------------------------------------


async def test_make_phone_tools_returns_expected_tools(ctx: AppContext):
    ctx = _make_ctx(ctx)
    tools = make_phone_tools(ctx)
    names = {t.name for t in tools}
    assert names == {"make_phone_call", "get_call_status"}
