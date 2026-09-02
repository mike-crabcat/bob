"""Bob3 repositories — the only code allowed to touch the core tables.

Lease/claim logic lives behind these interfaces (plan ground rule); SQL is
kept ANSI/Postgres-portable. Each method accepts an optional ``txn``
(``server.database.Transaction``) so callers can compose repository
operations with source-table writes in one atomic unit (invariant 2).
"""

from server.repositories.event_log import Event, EventLogRepository, new_event_id
from server.repositories.turns import TurnRepository
from server.repositories.effects import EffectRepository
from server.repositories.contacts import ContactRepository
from server.repositories.history import HistoryRepository

__all__ = [
    "Event",
    "EventLogRepository",
    "TurnRepository",
    "EffectRepository",
    "ContactRepository",
    "HistoryRepository",
    "new_event_id",
]
