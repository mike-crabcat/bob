"""Twilio callback signature validation (2026-10-12).

/phone/twiml and /phone/status ride the public funnel. Twilio signs the
PUBLIC url it called (phone.base_url + path), not the 127.0.0.1 URL the app
sees behind the funnel — the validator must reconstruct that."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from server.config import PhoneSettings, Settings
from server.main import create_app

TOKEN = "twilio-auth-token-test"
BASE = "https://example.ts.net"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "bob.db",
        phone=PhoneSettings(enabled=True, twilio_auth_token=TOKEN, base_url=BASE),
    )


PARAMS = {"CallSid": "CAtest", "CallStatus": "ringing", "CallDuration": "0"}


def _sig(path: str, params: dict) -> str:
    return RequestValidator(TOKEN).compute_signature(f"{BASE}{path}", params)


def test_enforce_rejects_forged_and_accepts_signed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOB_TWILIO_SIGNATURE", "enforce")
    with TestClient(create_app(_settings(tmp_path))) as client:
        forged = client.post("/phone/status", data=PARAMS,
                             headers={"X-Twilio-Signature": "bogus"})
        assert forged.status_code == 403
        missing = client.post("/phone/status", data=PARAMS)
        assert missing.status_code == 403
        signed = client.post("/phone/status", data=PARAMS, headers={
            "X-Twilio-Signature": _sig("/phone/status", PARAMS)})
        assert signed.status_code == 200


def test_enforce_on_twiml(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOB_TWILIO_SIGNATURE", "enforce")
    params = {"CallSid": "CAx", "Direction": "outbound-api", "From": "+61400000000"}
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.post("/phone/twiml", data=params).status_code == 403
        ok = client.post("/phone/twiml", data=params, headers={
            "X-Twilio-Signature": _sig("/phone/twiml", params)})
        assert ok.status_code == 200 and "<Stream" in ok.text


def test_log_mode_never_blocks(tmp_path, monkeypatch) -> None:
    import server.routers.phone as phone
    monkeypatch.setenv("BOB_TWILIO_SIGNATURE", "log")
    seen: list[str] = []
    real_info = phone.logger.info
    monkeypatch.setattr(phone.logger, "info",
                        lambda msg, *a, **k: (seen.append(msg % a if a else msg),
                                              real_info(msg, *a, **k)))
    with TestClient(create_app(_settings(tmp_path))) as client:
        r = client.post("/phone/status", data=PARAMS,
                        headers={"X-Twilio-Signature": "bogus"})
        assert r.status_code == 200
    assert any("signature INVALID" in m for m in seen)


def test_signature_uses_the_public_base_not_the_local_url(tmp_path) -> None:
    from server.routers.phone import twilio_signature_ok
    s = _settings(tmp_path)
    good = _sig("/phone/status", PARAMS)
    assert twilio_signature_ok(s, "/phone/status", "", PARAMS, good) is True
    local = RequestValidator(TOKEN).compute_signature(
        "http://127.0.0.1:8420/phone/status", PARAMS)
    assert twilio_signature_ok(s, "/phone/status", "", PARAMS, local) is False


def test_no_auth_token_means_unchecked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOB_TWILIO_SIGNATURE", "enforce")
    s = Settings(data_dir=tmp_path / "d", config_dir=tmp_path / "c",
                 db_path=tmp_path / "d" / "bob.db",
                 phone=PhoneSettings(enabled=True, base_url=BASE))
    from server.routers.phone import twilio_signature_ok
    assert twilio_signature_ok(s, "/phone/status", "", PARAMS, "x") is None
