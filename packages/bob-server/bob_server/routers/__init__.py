"""FastAPI routers for Bob."""

from bob_server.routers import calendars, contacts, context, persona, webhooks

__all__ = [
    "calendars",
    "contacts",
    "context",
    "persona",
    "webhooks",
]
