"""WebSocket client connecting to the whatsappbridge Go companion service.

Split into:
- ``_media``: pure-function helpers for phone/JID/timestamp formatting and media prep
- ``_group_events``: GroupEventsMixin (group sync handlers)
- ``_slash_commands``: SlashCommandsMixin (slash commands)
- ``_service``: WhatsAppBridgeService class

Re-exports ``WhatsAppBridgeService`` and ``prepare_media`` for backward compat
with ``from server.services.whatsapp_bridge_service import ...``.
"""

from __future__ import annotations

from server.services.whatsapp_bridge_service._media import (
    jid_to_phone, format_created_at, _resize_gif, prepare_media,
)
from server.services.whatsapp_bridge_service._service import WhatsAppBridgeService


__all__ = [
    "WhatsAppBridgeService",
    "jid_to_phone", "format_created_at", "_resize_gif", "prepare_media",
]
