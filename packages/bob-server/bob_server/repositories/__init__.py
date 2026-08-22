"""Bob3 repositories — the only code allowed to touch the core tables.

Lease/claim logic lives behind these interfaces (plan ground rule); SQL is
kept ANSI/Postgres-portable. Each method accepts an optional ``txn``
(``bob_server.database.Transaction``) so callers can compose repository
operations with source-table writes in one atomic unit (invariant 2).
"""

from bob_server.repositories.event_log import Event, EventLogRepository, new_event_id
from bob_server.repositories.turns import TurnRepository
from bob_server.repositories.effects import EffectRepository

__all__ = [
    "Event",
    "EventLogRepository",
    "TurnRepository",
    "EffectRepository",
    "new_event_id",
]
