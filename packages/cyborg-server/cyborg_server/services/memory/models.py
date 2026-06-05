"""Memory v6 data models.

All memory documents use YAML frontmatter in markdown files.  The canonical
entities (contacts, channels, trips, etc.) are referenced by stable IDs like
``contact-7c9f0fd7`` or ``channel-whatsapp-group-12036342829458``.

The v6 pipeline is:
  channel  ->  bulletin  ->  claim  ->  entity document
  (source)    (record)     (atom)     (derived view)
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
    # yaml.dump always ends with ``\\n``; ensure a single trailing newline
    # before the closing fence.
    dumped = dumped.rstrip("\n") + "\n"
    return f"---\n{dumped}---\n\n{body}"

# ---------------------------------------------------------------------------
# Entity types & helpers
# ---------------------------------------------------------------------------

ENTITY_CATEGORIES: tuple[str, ...] = (
    "contacts",
    "groups",
    "channels",
    "trips",
    "locations",
    "events",
    "tasks",
    "artifacts",
    "decisions",
)


def _empty_entity_dict() -> dict[str, list]:
    """Return a dict with all entity categories initialised to empty lists."""
    return {cat: [] for cat in ENTITY_CATEGORIES}


CLAIM_TYPES: tuple[str, ...] = (
    "fact",
    "preference",
    "constraint",
    "decision",
    "task",
    "availability",
    "booking",
    "artifact",
    "relationship",
    "private_note",
)

CLAIM_STATUSES: tuple[str, ...] = (
    "active",
    "superseded",
    "retracted",
    "expired",
    "disputed",
    "archived",
)

VISIBILITY_LEVELS: tuple[str, ...] = (
    "private",
    "contact",
    "group",
    "channel",
    "public",
)

ENTITY_TYPES: tuple[str, ...] = (
    "channel",
    "contact",
    "group",
    "location",
    "trip",
    "event",
    "task",
    "artifact",
    "decision",
)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EntityRef:
    """A lightweight reference to an entity within a bulletin or other document.

    Attributes:
        id: Canonical entity ID (e.g. ``contact-7c9f0fd7``).
        display_name: Human-readable label, if known.
        resolution_status: One of ``known`` / ``unresolved`` / ``ambiguous``.
        role: Free-form role descriptor in the context where the ref appears
              (e.g. ``"organizer"``, ``"attendee"``).
    """

    id: str
    display_name: str | None = None
    resolution_status: str = "known"
    role: str | None = None


@dataclass(slots=True)
class Bulletin:
    """An immutable plain-text memory note.

    Bulletins are simple factual observations. Contacts are referenced inline
    using ``{{contact:ID|Name}}`` tags. All entity resolution and claim
    extraction happens downstream in the dream pipeline.

    Attributes:
        id: Unique bulletin identifier (e.g. ``bulletin-20260531-a1b2c3``).
        created_at: When the bulletin was generated.
        channel_id: The channel this bulletin was sourced from.
        source_type: Origin type (``"session"``, ``"manual"``, ``"seed"``, etc.).
        source_id: Identifier for the specific source (e.g. session key).
        visibility: Who can see this bulletin.
        content: Plain-text bulletin body with contact tags.
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

    Claims are the core unit of memory.  Each claim expresses a single
    proposition (fact, preference, constraint, etc.) about a subject entity.

    Attributes:
        id: Unique claim identifier (e.g. ``claim-abc123``).
        type: Semantic type of the claim.
        subject_id: The entity this claim is about.
        predicate: Short verb phrase (e.g. ``"prefers_contact_via"``).
        object_id: Optional target entity or value.
        status: Lifecycle status.
        source_bulletins: IDs of bulletins this claim was derived from.
        visibility: Who can see this claim.
        scope: Scoping tags.
        created_at: When the claim was created.
        superseded_by: IDs of claims that supersede this one.
    """

    id: str
    type: str
    subject_id: str
    predicate: str
    object_id: str | None = None
    status: str = "active"
    source_bulletins: list[str] = field(default_factory=list)
    visibility: str = "channel"
    scope: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    superseded_by: list[str] = field(default_factory=list)
    body: str = ""


@dataclass(slots=True)
class EntityDocument:
    """Derived current-state view of an entity.

    Entity documents are the user-facing representation of a person, channel,
    trip, etc.  They are assembled from active claims and stored as markdown
    files with YAML frontmatter.

    Attributes:
        entity_id: Canonical entity ID.
        entity_type: One of the recognised entity types.
        display_name: Human-readable name.
        status: Entity lifecycle status (e.g. ``"active"``, ``"archived"``).
        extra_frontmatter: Type-specific fields stored in frontmatter
            (``channel_type``, ``artifact_type``, etc.).
        body: The full markdown body of the entity document.
        related_entities: Flat lists of entity IDs keyed by category.
        source_bulletins: Bulletin IDs that contributed to this entity view.
    """

    entity_id: str
    entity_type: str
    display_name: str
    status: str = "active"
    extra_frontmatter: dict = field(default_factory=dict)
    body: str = ""
    related_entities: dict[str, list[str]] = field(default_factory=_empty_entity_dict)
    source_bulletins: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QueryContext:
    """Context for a memory query, used to enforce access control.

    Attributes:
        actor: The contact ID of the entity making the query, or ``None``
               for system-level access.
        channel_id: The channel context of the query, if any.
        allowed_scopes: Scopes the actor is permitted to access.
    """

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
    """Compact input for the bulletin generation prompt.

    Attributes:
        session_key: The session this transcript comes from.
        messages: Messages with sender contact IDs and timestamps.
        participants: Contact ID/name pairs for group context.
    """

    session_key: str
    messages: list[BulletinMessage] = field(default_factory=list)
    participants: list[dict[str, str]] = field(default_factory=list)
