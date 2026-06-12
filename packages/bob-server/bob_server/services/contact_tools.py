"""Contact tools — shared contact search for use across dispatch contexts."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from bob_server.services.tools import tool

if TYPE_CHECKING:
    from bob_server.context import AppContext

logger = logging.getLogger(__name__)


def make_contact_tools(ctx: AppContext) -> list:
    """Create contact-related tools.

    Tools: search_contacts.
    """

    @tool
    async def search_contacts(query: str, limit: int = 5) -> str:
        """Search contacts by name, phone number, or email.
        Returns matching contacts with their ID, name, phone, and trusted status."""
        db = ctx.db
        pattern = f"%{query}%"
        rows = await db.fetch_all(
            """
            SELECT id, name, phone_number, email, is_trusted
            FROM contacts
            WHERE deleted_at IS NULL
              AND (name LIKE ? OR phone_number LIKE ? OR email LIKE ?)
            ORDER BY name
            LIMIT ?
            """,
            (pattern, pattern, pattern, limit),
        )
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

    return [search_contacts]
