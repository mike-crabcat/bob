"""Tests for the shared phone normalizer."""

from __future__ import annotations

import pytest

from bob_server.services.phone_utils import normalize_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Australian local landline forms
        ("(08) 9244 5300", "+61892445300"),
        ("0892445300", "+61892445300"),
        ("08 9244 5300", "+61892445300"),
        # Already international
        ("+61892445300", "+61892445300"),
        ("+61 8 9244 5300", "+61892445300"),
        # Bare Australian international
        ("61892445300", "+61892445300"),
        ("61456224867", "+61456224867"),
        # Mobile with leading 0
        ("0405 407 377", "+61405407377"),
        # WhatsApp JIDs (user and device forms)
        ("61456224867@s.whatsapp.net", "+61456224867"),
        ("61456224867:12@s.whatsapp.net", "+61456224867"),
        ("+61456224867@s.whatsapp.net", "+61456224867"),
        # Bare non-Australian international (>8 digits)
        ("44 20 7946 0000", "+442079460000"),
        ("442079460000", "+442079460000"),
        # Whitespace tolerance
        ("  0892445300  ", "+61892445300"),
    ],
)
def test_normalize_phone_valid(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",          # empty
        "   ",       # whitespace only
        "abc",       # no digits
        "92445300",  # 8-digit bare local: no area code, not credible — refuse
        "12345",     # too short
        "12345678",  # exactly 8 bare digits, no 0/61 prefix — refuse
        "@s.whatsapp.net",  # JID with empty local part
    ],
)
def test_normalize_phone_rejects(raw: str) -> None:
    assert normalize_phone(raw) is None
