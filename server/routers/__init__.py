"""FastAPI routers for Bob."""

from server.routers import calendars, contacts, context, persona, webhooks

__all__ = [
    "calendars",
    "contacts",
    "context",
    "persona",
    "webhooks",
]
