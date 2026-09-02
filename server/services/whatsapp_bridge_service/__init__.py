"""WebSocket client connecting to the whatsappbridge Go companion service.

Split into:
- ``_media``: pure-function helpers for phone/JID/timestamp formatting and media prep
- ``_group_events``: GroupEventsMixin (group sync handlers)
- ``_slash_commands``: SlashCommandsMixin (slash commands)
- ``_service``: WhatsAppBridgeService class

Re-exports ``WhatsAppBridgeService`` and ``_prepare_media`` for backward compat
with ``from bob_server.services.whatsapp_bridge_service import ...``.
"""

from __future__ import annotations

from bob_server.services.whatsapp_bridge_service._media import (
    _jid_to_phone, _format_created_at, _resize_gif, _prepare_media,
)
from bob_server.services.whatsapp_bridge_service._service import WhatsAppBridgeService


__all__ = [
    "WhatsAppBridgeService",
    "_jid_to_phone", "_format_created_at", "_resize_gif", "_prepare_media",
]
