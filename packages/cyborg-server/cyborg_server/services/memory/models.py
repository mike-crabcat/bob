"""Memory v7 data models.

Claims are the source of truth. Entity documents are minimal records
(identity + display name). The rendered view is generated on demand by
the template renderer in claim_types.py.

The v7 pipeline is:
  channel  ->  bulletin  ->  claim  ->  entity record
  (source)    (record)     (atom)     (identity)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

import yaml

# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown.

    Returns ``(frontmatter_dict, body)`` where *body* is everything after the
    closing ``---`` fence (leading blank lines stripped).  If no frontmatter
    fence is found the whole *text* is returned as the body with an empty dict.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1)) or {}
    body = text[m.end():].lstrip("\n")
    return fm, body


def serialize_frontmatter(fm: dict, body: str) -> str:
    """Serialize a frontmatter dict + body back to a markdown string."""
    if not fm:
        return body
    dumped = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    dumped = dumped.rstrip("\n") + "\n"
    return f"---\n{dumped}---\n\n{body}"

# ---------------------------------------------------------------------------
# Entity types & helpers
# ---------------------------------------------------------------------------

ENTITY_TYPES: tuple[str, ...] = (
    "person", "group", "location", "trip", "tripstop",
    "transport", "event", "task", "file", "thing", "decision",
)


CLAIM_STATUSES: tuple[str, ...] = (
    "active",
    "superseded",
    "retracted",
    "expired",
    "disputed",
    "archived",
    "redundant",
    "disproven",
    "obsolete",
)

VISIBILITY_LEVELS: tuple[str, ...] = (
    "private",
    "contact",
    "group",
    "channel",
    "public",
)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EntityRef:
    """A lightweight reference to an entity within a bulletin or other document."""

    id: str
    display_name: str | None = None
    resolution_status: str = "known"
    role: str | None = None


@dataclass(slots=True)
class Bulletin:
    """An immutable plain-text memory note.

    Bulletins are simple factual observations. Contacts are referenced inline
    using ``{{contact:ID|Name}}`` tags.
    """

    id: str
    created_at: datetime
    channel_id: str
    source_type: str
    source_id: str
    visibility: str = "channel"
    content: str = ""


@dataclass(slots=True)
class Claim:
    """An atomic, typed memory extracted from one or more bulletins.

    Each claim expresses a single proposition using a predefined claim type.
    Entity references go in object_id; scalar values go in value.
    """

    id: str
    claim_type_key: str
    subject_id: str
    object_id: str | None = None
    value: str | None = None
    status: str = "active"
    source_bulletins: list[str] = field(default_factory=list)
    visibility: str = "channel"
    scope: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    superseded_by: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EntityDocument:
    """Minimal entity record — identity only.

    The rendered view (body) is generated on demand from claims
    using the template renderer in claim_types.py.
    """

    entity_id: str
    entity_type: str
    display_name: str
    status: str = "active"
    source_bulletins: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QueryContext:
    """Context for a memory query, used to enforce access control."""

    actor: str | None = None
    channel_id: str | None = None
    allowed_scopes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BulletinMessage:
    """A single message in the bulletin generator input."""

    sender_contact_id: str
    timestamp: str
    content: str


@dataclass(slots=True)
class BulletinGeneratorInput:
    """Compact input for the bulletin generation prompt."""

    session_key: str
    messages: list[BulletinMessage] = field(default_factory=list)
    participants: list[dict[str, str]] = field(default_factory=list)
