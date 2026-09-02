"""Contact tools — shared contact search/creation for dispatch contexts."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from server.services.phone_utils import normalize_phone
from server.services.tools import tool

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)


def make_contact_tools(ctx: AppContext, *, is_trusted: bool = False) -> list:
    """Create contact-related tools.

    Tools: search_contacts (all sessions), create_contact (trusted only).
    """

    @tool
    async def search_contacts(query: str, limit: int = 5) -> str:
        """Search contacts by name, phone number, or email.
        Returns matching contacts with their ID, name, phone, and trusted status."""
        db = ctx.db
        from server.repositories.contacts import ContactRepository
        rows = await ContactRepository(db).search(f"%{query}%", limit)
        results = [
            {
                "id": row["id"],
                "name": row["name"],
                "phone_number": row["phone_number"],
                "email": row.get("email"),
                "is_trusted": bool(row.get("is_trusted", 0)),
            }
            for row in rows
        ]
        return json.dumps(results)

    @tool
    async def create_contact(name: str, phone_number: str) -> str:
        """Add a new contact so Bob can call or otherwise reach them.

        Use this when a task needs an outbound contact that doesn't exist yet
        (e.g. calling a shop whose number you just looked up). The contact is
        outbound-only: it appears in search results and can be called via
        create_subagent(agent_type="openai_voice", modality="phone"), but its
        number cannot message Bob until inbound access is granted separately.

        - name: display name, e.g. "JB Hi-Fi Osborne Park"
        - phone_number: any common Australian or international form
          ("(08) 9244 5300", "0405 407 377", "+44 20 7946 0000"); it is
          normalized automatically.
        Returns the contact's id (pass as contact_id to the voice subagent).
        """
        db = ctx.db
        clean_name = name.strip()
        if not clean_name:
            return json.dumps({
                "error": "name must not be blank",
            })
        normalized = normalize_phone(phone_number)
        if normalized is None:
            return json.dumps({
                "error": (
                    f"could not parse phone number: {phone_number!r} — use a "
                    "full form like (08) 9244 5300, 0405 407 377, or +44 20 7946 0000"
                ),
            })

        from server.repositories.contacts import ContactRepository
        existing = await ContactRepository(db).get_by_phone(normalized)
        if existing:
            # Never mutate an existing contact from here: a real person must not
            # lose inbound access (or their name) because a task "added" them.
            logger.info(
                "create_contact: phone %s already exists as %s — returning existing",
                normalized, existing["id"],
            )
            return json.dumps({
                "contact_id": existing["id"],
                "created": False,
                "name": existing["name"],
                "phone_number": existing["phone_number"],
                "is_trusted": bool(existing.get("is_trusted", 0)),
                "allow_inbound_dm": bool(existing.get("allow_inbound_dm", 1)),
            })

        from server.repositories.contacts import ContactRepository
        contact_id = await ContactRepository(db).create(
            name=clean_name, phone_number=normalized, is_trusted=0, allow_inbound_dm=0)

        from server.services.memory import MemoryService
        try:
            await MemoryService(ctx).ensure_person_entry(
                ctx.settings.harness.workspace_dir,
                contact_id=contact_id,
                name=clean_name,
                phone_number=normalized,
            )
        except Exception:
            # The directory row is the source of truth for calls; a memory
            # link failure shouldn't fail the task that asked for the contact.
            logger.exception("create_contact: person entry sync failed for %s", contact_id)

        logger.info("create_contact: created %s (%s) for phone %s", contact_id, clean_name, normalized)
        return json.dumps({
            "contact_id": contact_id,
            "created": True,
            "name": clean_name,
            "phone_number": normalized,
            "is_trusted": False,
            "allow_inbound_dm": False,
        })

    tools = [search_contacts]
    if is_trusted:
        tools.append(create_contact)
    return tools
