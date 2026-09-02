"""Channel inbound policies + ContactResolver (Bob3 Phase II).

Behaviour-preserving extraction of the sender-resolution logic from the
WhatsApp bridge and email poller. Channel asymmetries are deliberate and
pinned by the Phase 0 characterization suites — do not "unify" them:

- WhatsApp DMs from unknown numbers are DROPPED; group senders are
  auto-seeded untrusted. Contacts with allow_inbound_dm=0 are dropped in DMs.
- Email auto-seeds ALL unknown senders untrusted; a missing sender address
  stays external/untrusted with no contact row.
- WhatsApp seeding and name backfill sync person memory; email seeding
  does NOT (existing behaviour).

Shared mechanics (lookup, seeding, backfill) live in ContactResolver over
ContactRepository; the accept/drop/trust decisions stay per-channel here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from server.repositories.contacts import ContactRepository

logger = logging.getLogger(__name__)


class ContactResolver:
    """Lookup / auto-seed / name-backfill over ContactRepository."""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.contacts = ContactRepository(ctx.db)

    async def seed_untrusted_by_phone(
            self, phone_number: str, display_name: str = "",
            *, channel: str = "WhatsApp") -> str:
        """Create an untrusted contact for a phone sender + person memory entry."""
        name = display_name or phone_number
        contact_id = await self.contacts.create(name=name, phone_number=phone_number)
        logger.info("auto-seeded untrusted contact %s for phone %s",
                    contact_id, phone_number)
        from server.services.memory import MemoryService
        await MemoryService(self.ctx).ensure_person_entry(
            self.ctx.settings.harness.workspace_dir,
            contact_id=contact_id, name=name,
            phone_number=phone_number, channel=channel,
        )
        return contact_id

    async def seed_untrusted_by_email(self, email: str, display_name: str = "") -> str:
        """Create an untrusted contact for an email sender (no memory sync)."""
        contact_id = await self.contacts.create(
            name=display_name or email, email=email)
        logger.debug("auto-seeded untrusted contact %s for email %s",
                     contact_id, email)
        return contact_id

    async def backfill_name_from_channel(
            self, contact: dict, sender_name: str, placeholder: str) -> None:
        """If the contact's name is empty or a bare identifier, adopt the
        channel-provided display name and sync person memory."""
        if sender_name and contact["name"] in ("", placeholder):
            await self.contacts.update_name(contact["id"], sender_name)
            from server.services.memory import MemoryService
            await MemoryService(self.ctx).sync_person_display_name_for_contact(
                contact["id"], sender_name)


@dataclass
class SenderResolution:
    accepted: bool
    contact_id: str | None = None
    is_trusted: bool = False
    drop_reason: str | None = None  # "inbound_disabled" | "unknown_dm"


class WhatsAppInboundPolicy:
    """Acceptance/trust/seeding rules for inbound WhatsApp messages."""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.resolver = ContactResolver(ctx)

    async def resolve_sender(
            self, *, chat_kind: str, phone_number: str, sender_name: str,
    ) -> SenderResolution:
        contact = await self.resolver.contacts.get_by_phone(phone_number)

        if contact and chat_kind == "dm" and not bool(contact.get("allow_inbound_dm", 1)):
            # Contact exists but is outbound-only (agent-created for a call,
            # or operator-restricted). Same treatment as unknown numbers.
            return SenderResolution(
                accepted=False, contact_id=contact["id"],
                drop_reason="inbound_disabled")

        if contact:
            contact_id = contact["id"]
            is_trusted = bool(contact.get("is_trusted", 0))
            logger.info("resolved contact %s (trusted=%s) for phone %s",
                        contact_id, is_trusted, phone_number)
            await self.resolver.backfill_name_from_channel(
                contact, sender_name, placeholder=phone_number)
            return SenderResolution(
                accepted=True, contact_id=contact_id, is_trusted=is_trusted)

        if chat_kind == "dm":
            # Security gate: drop DMs from numbers with no contact row.
            # Group sync auto-seeds contacts for everyone Bob has seen in a
            # group, so any legitimate acquaintance already has a row.
            return SenderResolution(accepted=False, drop_reason="unknown_dm")

        # Group message from an unknown sender: auto-seed untrusted.
        logger.info("no contact found for phone %s", phone_number)
        contact_id = await self.resolver.seed_untrusted_by_phone(
            phone_number, sender_name)
        return SenderResolution(
            accepted=True, contact_id=contact_id, is_trusted=False)


@dataclass
class EmailSenderResolution:
    contact_id: str | None
    is_trusted: bool


class EmailInboundPolicy:
    """Trust/seeding rules for inbound email. Email never drops on sender:
    unknown senders are seeded untrusted; a missing address stays external."""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.resolver = ContactResolver(ctx)

    async def resolve_sender(
            self, sender_email: str | None, sender_name: str | None,
    ) -> EmailSenderResolution:
        if not sender_email:
            return EmailSenderResolution(contact_id=None, is_trusted=False)
        contact = await self.resolver.contacts.get_by_email(sender_email)
        if contact:
            return EmailSenderResolution(
                contact_id=contact["id"],
                is_trusted=bool(contact.get("is_trusted", 0)))
        contact_id = await self.resolver.seed_untrusted_by_email(
            sender_email, sender_name or "")
        return EmailSenderResolution(contact_id=contact_id, is_trusted=False)
