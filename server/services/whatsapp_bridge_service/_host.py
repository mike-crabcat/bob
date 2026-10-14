"""What the bridge mixins may use from their host class (type-checking only).

GroupEventsMixin, ReactionsMixin and SlashCommandsMixin are only ever mixed
into WhatsAppBridgeService; they call its BaseService attributes and a few
of its methods. Each mixin subclasses BridgeHost under TYPE_CHECKING so
pyright can check those calls; at runtime the mixins subclass ``object``.
_service.py asserts WhatsAppBridgeService still satisfies this protocol, so
a signature change there fails the type check instead of drifting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from server.config import Settings
    from server.context import AppContext
    from server.database import Database
    from server.services.dispatch_runner import DispatchSpec


class BridgeHost(Protocol):
    ctx: AppContext
    db: Database

    def _get_settings(self) -> Settings: ...

    async def send_message(self, chat_id: str, text: str, *,
                           reply_to: str | None = None) -> str: ...

    def _track_send(self, send_records: list[Any], request_id: str,
                    text: str) -> None: ...

    async def _send_ack(self, message_id: str) -> None: ...

    async def _resolve_or_seed_contact(
        self, phone_number: str, display_name: str = "",
    ) -> tuple[str, bool]: ...

    async def _resolve_whatsapp_sender(
        self, chat_id: str, chat_kind: str, sender_jid: str, sender_name: str,
        *, preview: str = "",
    ) -> tuple[str, str | None, bool] | None: ...

    async def _derive_whatsapp_session(
        self, chat_id: str, chat_kind: str, sender_jid: str, phone_number: str,
    ) -> tuple[str, str]: ...

    async def _register_whatsapp_participant(
        self, session_key: str, chat_id: str, chat_kind: str,
        phone_number: str, sender_name: str,
        contact_id: str | None, is_trusted: bool,
    ) -> None: ...

    async def _build_inbound_dispatch_spec(
        self, *, session_key: str, chat_id: str, chat_kind: str,
        contact_id: str | None, is_trusted: bool, human_initiated: bool,
        sender_name: str = "", text_preview: str = "",
        extra_system_note: str = "", inbound_text: str | None = None,
        call_category: str = "whatsapp_incoming",
    ) -> DispatchSpec: ...
