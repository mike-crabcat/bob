"""Shared phone-number normalization.

Single home for the +CC-defaulting logic previously duplicated (and drifting)
between ``routers/contacts.py`` and ``whatsapp_bridge_service/_media.py``.
Australian-defaulting: bare local forms are assumed +61, matching the primary
deployment. Numbers too short to be real (after JID/device stripping) return
None rather than being mangled into a plausible-but-wrong number.
"""

from __future__ import annotations

import re


def normalize_phone(raw: str) -> str | None:
    """Normalize a phone number (or WhatsApp JID) to +CC format.

    Handles: "+CC…" explicit international, "0…" Australian local (→ +61),
    "61…" bare Australian international, bare >8-digit international, and
    WhatsApp JIDs ("61456224867@s.whatsapp.net", "…:12@…"). Returns None for
    anything that doesn't reduce to a credible number.
    """
    if not raw:
        return None
    # Strip WhatsApp JID domain and device suffix before anything else.
    phone_part = raw.split("@", 1)[0]
    phone_part = phone_part.split(":", 1)[0]
    had_plus = phone_part.lstrip().startswith("+")
    digits = re.sub(r"\D", "", phone_part)
    if not digits:
        return None
    if had_plus:
        return "+" + digits
    if digits.startswith("0"):
        return "+61" + digits[1:]
    if digits.startswith("61"):
        return "+" + digits
    if len(digits) > 8:
        return "+" + digits
    return None
