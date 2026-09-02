"""Tests for call placement — initiate_outbound_call (voice_dispatch_service)."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bob_server.config import PhoneSettings, Settings
from bob_server.context import AppContext
from bob_server.services.voice_dispatch_service import call_agendas, initiate_outbound_call


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
    with patch.dict(sys.modules, {"twilio": mock_twilio, "twilio.rest": mock_twilio_rest}), \
         patch("bob_server.services.realtime_prewarm.start_prewarm"):
        # start_prewarm is patched out: it would open REAL OpenAI sessions
        # from the test process using the configured API key.
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
            realtime_meta={"instructions": "say hi", "voice": "", "subagent_id": "sub-x"},
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

    # Dispatch metadata is persisted durably (survives a restart), not just cached
    assert json.loads(row["realtime_meta"])["instructions"] == "say hi"
    assert row["subagent_id"] == "sub-x"

    # Verify the in-memory cache was populated too
    assert "CA_test_sid" in call_agendas
    assert call_agendas["CA_test_sid"]["agenda"] == "Test agenda"

    # Cleanup
    call_agendas.pop("CA_test_sid", None)


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
            twiml=(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<Response>\n"
                "  <Connect>\n"
                '    <Stream url="wss://myserver.ngrok.io/phone/media" />\n'
                "  </Connect>\n"
                "</Response>"
            ),
            status_callback="https://myserver.ngrok.io/phone/status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )

    # Cleanup
    call_agendas.pop("CA_params", None)
