"""Tests for phone.py — initiate_outbound_call, /call endpoint, /status webhook."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cyborg_server.config import PhoneSettings, Settings
from cyborg_server.context import AppContext
from cyborg_server.routers.phone import _call_agendas, initiate_outbound_call


def _make_phone_settings(*, enabled: bool = True, base_url: str = "https://example.com") -> PhoneSettings:
    return PhoneSettings(
        enabled=enabled,
        twilio_account_sid="ACtest",
        twilio_auth_token="test_token",
        twilio_phone_number="+61400000000",
        base_url=base_url,
    )


def _make_settings(phone: PhoneSettings | None = None) -> Settings:
    settings = Settings.from_env()
    object.__setattr__(settings, "phone", phone or _make_phone_settings())
    return settings


@contextmanager
def _mock_twilio():
    """Inject a fake twilio.rest module so lazy imports resolve without the real package."""
    mock_twilio = MagicMock()
    mock_twilio_rest = MagicMock()
    with patch.dict(sys.modules, {"twilio": mock_twilio, "twilio.rest": mock_twilio_rest}):
        yield mock_twilio_rest.Client


# ---------------------------------------------------------------------------
# initiate_outbound_call
# ---------------------------------------------------------------------------


async def test_initiate_outbound_call_returns_expected_shape(ctx: AppContext):
    phone_settings = _make_phone_settings()
    settings = _make_settings(phone_settings)

    mock_call = MagicMock()
    mock_call.sid = "CA_test_sid"
    mock_call.status = "ringing"

    with _mock_twilio() as MockClient:
        MockClient.return_value.calls.create.return_value = mock_call

        result = await initiate_outbound_call(
            db=ctx.db,
            settings=settings,
            phone_settings=phone_settings,
            to_number="+61400123456",
            agenda="Test agenda",
        )

    assert result["call_sid"] == "CA_test_sid"
    assert result["status"] == "ringing"
    assert "call_id" in result

    # Verify DB record was created
    row = await ctx.db.fetch_one("SELECT * FROM phone_calls WHERE call_sid = ?", ("CA_test_sid",))
    assert row is not None
    assert row["phone_number"] == "+61400123456"
    assert row["status"] == "ringing"
    assert row["agenda"] == "Test agenda"
    assert row["direction"] == "outbound"

    # Verify _call_agendas was populated
    assert "CA_test_sid" in _call_agendas
    assert _call_agendas["CA_test_sid"]["agenda"] == "Test agenda"

    # Cleanup
    _call_agendas.pop("CA_test_sid", None)


async def test_initiate_outbound_call_disabled(ctx: AppContext):
    phone_settings = _make_phone_settings(enabled=False)
    settings = _make_settings(phone_settings)

    result = await initiate_outbound_call(
        db=ctx.db,
        settings=settings,
        phone_settings=phone_settings,
        to_number="+61400123456",
        agenda="Test",
    )

    assert result == {"error": "Phone subsystem is not enabled"}


async def test_initiate_outbound_call_passes_twilio_params(ctx: AppContext):
    phone_settings = _make_phone_settings(base_url="https://myserver.ngrok.io")
    settings = _make_settings(phone_settings)

    mock_call = MagicMock()
    mock_call.sid = "CA_params"
    mock_call.status = "ringing"

    with _mock_twilio() as MockClient:
        mock_client = MockClient.return_value
        mock_client.calls.create.return_value = mock_call

        await initiate_outbound_call(
            db=ctx.db,
            settings=settings,
            phone_settings=phone_settings,
            to_number="+61400999888",
            agenda="Check params",
        )

        mock_client.calls.create.assert_called_once_with(
            to="+61400999888",
            from_="+61400000000",
            url="https://myserver.ngrok.io/phone/twiml",
            status_callback="https://myserver.ngrok.io/phone/status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )

    # Cleanup
    _call_agendas.pop("CA_params", None)
