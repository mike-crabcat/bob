"""Channel adapters — map session contexts to canonical channel IDs."""

from __future__ import annotations


def resolve_channel_id(session_key: str) -> str:
    """Derive a canonical channel ID from a session key.

    Session key formats:
        agent:main:whatsapp:group:12036342829458
        agent:main:whatsapp:dm:61456224867
        agent:main:email:thread-id
        agent:main:calendar:personal
        (anything else)

    Returns:
        channel-whatsapp-group-12036342829458
        channel-whatsapp-dm-61456224867
        channel-email-thread-id
        channel-calendar-personal
        channel-manual-notes  (fallback)
    """
    parts = session_key.split(":")
    if len(parts) >= 5 and parts[0] == "agent":
        medium = parts[2]
        kind = parts[3]
        identifier = parts[4]
        return f"channel-{medium}-{kind}-{identifier}"
    if len(parts) >= 4 and parts[0] == "agent":
        medium = parts[2]
        kind = parts[3]
        return f"channel-{medium}-{kind}"
    return "channel-manual-notes"


def derive_visibility(session_key: str) -> str:
    """Derive default visibility from session key."""
    parts = session_key.split(":")
    if len(parts) >= 4:
        kind = parts[3]
        if kind == "group":
            return "group"
        if kind == "dm":
            return "contact"
    return "private"


def derive_scope(session_key: str, contact_id: str | None = None) -> list[str]:
    """Derive default scope list from session key."""
    parts = session_key.split(":")
    scopes: list[str] = ["public"]

    if len(parts) >= 4:
        kind = parts[3]
        if kind == "group" and len(parts) >= 5:
            scopes.append(f"group-{parts[4]}")
        if kind == "dm" and contact_id:
            scopes.append(contact_id)

    return scopes


def derive_channel_type(session_key: str) -> str:
    """Derive channel type from session key."""
    parts = session_key.split(":")
    if len(parts) >= 3:
        medium = parts[2]
        kind = parts[3] if len(parts) >= 4 else "unknown"
        return f"{medium}_{kind}"
    return "unknown"


_CHANNEL_DISPLAY_NAMES: dict[str, str] = {}


def register_channel_display_name(channel_id: str, display_name: str) -> None:
    _CHANNEL_DISPLAY_NAMES[channel_id] = display_name


def get_channel_display_name(channel_id: str) -> str:
    return _CHANNEL_DISPLAY_NAMES.get(channel_id, channel_id)
