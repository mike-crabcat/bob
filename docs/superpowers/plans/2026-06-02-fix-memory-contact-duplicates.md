# Fix Memory Contact Entity Duplicates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate duplicate contact entity documents in `memory/entities/contact/` and ensure every contact entity is linked to a row in the cyborg contacts database (or explicitly marked as unresolved/non-DB).

**Architecture:** Two-layer fix. *Prevention* — feed the full contacts DB into every bulletin-generator call so the LLM uses canonical `contact-{hex8}` IDs; add a post-claim reconciliation step that re-maps any non-canonical contact subject_id (name-slugs, `unresolved-`, `unknown-`) to canonical via display_name lookup before entity documents are written. *Cleanup* — a one-shot CLI pass that walks the existing 34 contact entities, merges non-canonical duplicates into their canonical counterparts (preserving timeline/source-bulletins/related-entities), rewrites every claim file, bulletin entity-ref, and entity related-entities section to use canonical IDs, and writes a real `contact_id`/`email`/`phone_number` foreign-key block into canonical entity frontmatter.

**Tech Stack:** Python 3.11, SQLite (contacts DB at `~/.local/share/cyborg/cyborg.db`), PyYAML, pytest (asyncio), typer CLI.

---

## File Structure

**New files:**

- `packages/cyborg-server/cyborg_server/services/memory/contact_directory.py` — Loads contacts from DB once, provides: `canonical_id_by_uuid()`, `canonical_id_by_name()`, `contact_record_by_canonical_id()`. Single source of truth for contact lookups.
- `packages/cyborg-server/cyborg_server/services/memory/reconcile.py` — `reconcile_contact_id(subject_id, display_name, directory)` → canonical_id or unchanged. Pure function, no side effects.
- `packages/cyborg-server/cyborg_server/services/memory/cleanup.py` — One-shot cleanup pipeline: `build_renaming_map()`, `merge_entity_docs()`, `rewrite_claims()`, `rewrite_bulletins()`, `rewrite_entity_related()`, `run_cleanup()`.
- `packages/cyborg-server/tests/memory/__init__.py`
- `packages/cyborg-server/tests/memory/test_contact_directory.py`
- `packages/cyborg-server/tests/memory/test_reconcile.py`
- `packages/cyborg-server/tests/memory/test_cleanup.py`
- `packages/cyborg-server/tests/memory/test_entity_update_reconciliation.py`
- `packages/cyborg-server/tests/memory/conftest.py` — shared fixture: in-memory DB preloaded with 3 contacts (Blair Nicol, Mike Cleaver, Helen Burnside), a memory dir pre-populated to mirror the duplicate patterns.

**Modified files:**

- `packages/cyborg-server/cyborg_server/heartbeat.py:182-188` — Pass `known_entities` loaded from `ContactDirectory`.
- `packages/cyborg-server/cyborg_server/services/memory/prompts.py:77-110` — Strengthen the "Contact and Entity Rules" block: mandate using provided known contact IDs verbatim, forbid inventing `unresolved-`/`unknown-`/name-slug IDs when a match exists in `known_entities`.
- `packages/cyborg-server/cyborg_server/services/memory/service.py:336-363` — Call `reconcile_contact_id` on every claim subject_id/object_id before grouping. Also call from `_lookup_contact_names` to canonicalize existing display_name lookups.
- `packages/cyborg-server/cyborg_server/services/memory/service.py:197-214` — `write_entity`: if entity_type is `contact` and entity_id is canonical `contact-{hex8}` matching the directory, inject `contact_id` (full UUID), `email`, `phone_number` into frontmatter.
- `packages/cyborg-server/cyborg_server/cli.py` — Add `memory_app.command("cleanup-contacts")` invoking `run_cleanup()`.

---

## Task 1: ContactDirectory — load contacts from DB

**Files:**
- Create: `packages/cyborg-server/cyborg_server/services/memory/contact_directory.py`
- Create: `packages/cyborg-server/tests/memory/__init__.py`
- Create: `packages/cyborg-server/tests/memory/conftest.py`
- Create: `packages/cyborg-server/tests/memory/test_contact_directory.py`

- [ ] **Step 1: Write the failing test**

```python
# packages/cyborg-server/tests/memory/test_contact_directory.py
from __future__ import annotations

import pytest

from cyborg_server.services.memory.contact_directory import ContactDirectory


@pytest.mark.asyncio
async def test_directory_loads_contacts_by_uuid_and_name(memory_db):
    # memory_db fixture seeds three contacts: Blair Nicol, Mike Cleaver, Helen Burnside
    dir_ = await ContactDirectory.load(memory_db)

    blair = dir_.get_by_name("Blair Nicol")
    assert blair is not None
    assert blair.canonical_id == "contact-03f3902d"
    assert blair.uuid == "03f3902d-330b-4f15-bf2a-b1385a917677"
    assert blair.email == ""
    assert blair.phone_number == "+61401589328"

    by_id = dir_.get_by_canonical_id("contact-03f3902d")
    assert by_id is blair


@pytest.mark.asyncio
async def test_directory_case_insensitive_name_lookup(memory_db):
    dir_ = await ContactDirectory.load(memory_db)
    assert dir_.get_by_name("blair nicol").canonical_id == "contact-03f3902d"
    assert dir_.get_by_name("BLAIR").canonical_id == "contact-03f3902d"


@pytest.mark.asyncio
async def test_directory_first_name_only_falls_back_to_full_name_match(memory_db):
    """A first-name query returns the unique match if only one contact has that first name."""
    dir_ = await ContactDirectory.load(memory_db)
    # "Blair" is unique in the fixture
    assert dir_.get_by_name("Blair").canonical_id == "contact-03f3902d"
    # "Bob" does not exist
    assert dir_.get_by_name("Bob") is None
```

```python
# packages/cyborg-server/tests/memory/conftest.py
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from cyborg_server.database import Database


SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "cyborg_server" / "schemas"


@pytest.fixture
async def memory_db(tmp_path):
    """In-memory DB with three contacts mirroring real production data."""
    db = Database(db_path=Path(":memory:"), schema_dir=SCHEMA_DIR, pool_size=1)
    await db.connect()
    await db.apply_migrations()

    now = datetime.utcnow().isoformat()
    rows = [
        ("03f3902d-330b-4f15-bf2a-b1385a917677", "Blair Nicol",  "+61401589328", ""),
        ("7c9f0fd7-6134-4495-aa8c-f04f11bc15e8", "Mike Cleaver", "+61456224867", "mike@crabcat.com"),
        ("b5d279cf-4c4d-4d6c-a7af-18efc507845d", "Helen Burnside","+61456224866", "burnside.helen@gmail.com"),
    ]
    for uuid, name, phone, email in rows:
        await db.execute(
            "INSERT INTO contacts (id, name, phone_number, email, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uuid, name, phone, email, now, now),
        )
    yield db
    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_contact_directory.py -v
```
Expected: ImportError on `cyborg_server.services.memory.contact_directory`.

- [ ] **Step 3: Implement ContactDirectory**

```python
# packages/cyborg-server/cyborg_server/services/memory/contact_directory.py
"""Loads contacts from the cyborg contacts DB and provides name/UUID lookups."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContactRecord:
    uuid: str            # full UUID, e.g. "03f3902d-330b-4f15-bf2a-b1385a917677"
    canonical_id: str    # "contact-03f3902d"
    name: str
    phone_number: str
    email: str


class ContactDirectory:
    """In-memory lookup of all contacts in the cyborg contacts DB."""

    def __init__(self, records: list[ContactRecord]) -> None:
        self._by_canonical: dict[str, ContactRecord] = {r.canonical_id: r for r in records}
        self._by_uuid: dict[str, ContactRecord] = {r.uuid: r for r in records}
        # Map of lowercased name → list of records (handles ambiguity)
        self._by_name_lc: dict[str, list[ContactRecord]] = {}
        for r in records:
            self._by_name_lc.setdefault(r.name.lower(), []).append(r)
            first = r.name.split()[0].lower() if r.name else ""
            if first:
                self._by_name_lc.setdefault(first, []).append(r)

    @classmethod
    async def load(cls, db: Any) -> "ContactDirectory":
        rows = await db.fetch_all(
            "SELECT id, name, phone_number, email FROM contacts "
            "WHERE name IS NOT NULL AND name != '' AND deleted_at IS NULL"
        )
        records = []
        for r in rows:
            uuid = str(r["id"])
            records.append(ContactRecord(
                uuid=uuid,
                canonical_id=f"contact-{uuid[:8]}",
                name=r["name"],
                phone_number=r["phone_number"] or "",
                email=r["email"] or "",
            ))
        return cls(records)

    def get_by_canonical_id(self, canonical_id: str) -> ContactRecord | None:
        return self._by_canonical.get(canonical_id)

    def get_by_uuid(self, uuid: str) -> ContactRecord | None:
        return self._by_uuid.get(uuid)

    def get_by_name(self, name: str) -> ContactRecord | None:
        """Case-insensitive name lookup.

        Tries full-name match first, then first-name. Returns None if no match
        or if multiple distinct contacts share the name (ambiguous).
        """
        key = name.strip().lower()
        if not key:
            return None
        full_matches = self._by_name_lc.get(key, [])
        # Filter out first-name entries that aren't actually full-name matches
        full_only = [r for r in full_matches if r.name.lower() == key]
        if len(full_only) == 1:
            return full_only[0]
        if len(full_only) > 1:
            return None  # ambiguous
        # Fall back to first-name match (only if unique)
        first_only = [r for r in full_matches if r.name.split()[0].lower() == key]
        if len(first_only) == 1:
            return first_only[0]
        return None

    def all_canonical_ids(self) -> set[str]:
        return set(self._by_canonical.keys())

    def as_known_entities(self) -> dict[str, list[dict[str, str]]]:
        """Render as the `known_entities.contacts` hint for the bulletin generator."""
        return {
            "contacts": [
                {"id": r.canonical_id, "display_name": r.name}
                for r in self._by_canonical.values()
            ]
        }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_contact_directory.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/cyborg-server/cyborg_server/services/memory/contact_directory.py \
        packages/cyborg-server/tests/memory/
git commit -m "Add ContactDirectory for contacts DB lookups"
```

---

## Task 2: reconcile_contact_id — map non-canonical IDs to canonical

**Files:**
- Create: `packages/cyborg-server/cyborg_server/services/memory/reconcile.py`
- Create: `packages/cyborg-server/tests/memory/test_reconcile.py`

- [ ] **Step 1: Write the failing test**

```python
# packages/cyborg-server/tests/memory/test_reconcile.py
from __future__ import annotations

import pytest

from cyborg_server.services.memory.contact_directory import ContactDirectory, ContactRecord
from cyborg_server.services.memory.reconcile import reconcile_contact_id


def _directory_with_blair() -> ContactDirectory:
    return ContactDirectory([
        ContactRecord(
            uuid="03f3902d-330b-4f15-bf2a-b1385a917677",
            canonical_id="contact-03f3902d",
            name="Blair Nicol",
            phone_number="+61401589328",
            email="",
        ),
    ])


def test_canonical_id_unchanged():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("contact-03f3902d", "Blair Nicol", dir_) == "contact-03f3902d"


def test_name_slug_resolved_via_display_name():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("contact-blair-nicol", "Blair Nicol", dir_) == "contact-03f3902d"


def test_unresolved_resolved_via_display_name():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("unresolved-contact-blair", "Blair Nicol", dir_) == "contact-03f3902d"


def test_unknown_prefix_resolved_via_display_name():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("contact-unknown-blair", "Blair", dir_) == "contact-03f3902d"


def test_first_name_only_resolves_when_unique():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("contact-blair", "Blair", dir_) == "contact-03f3902d"


def test_no_directory_returns_input_unchanged():
    assert reconcile_contact_id("contact-blair-nicol", "Blair Nicol", None) == "contact-blair-nicol"


def test_no_display_name_returns_input_unchanged():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("contact-blair-nicol", "", dir_) == "contact-blair-nicol"


def test_display_name_not_in_db_preserves_id():
    """Genuine non-DB contacts keep their unresolved ID."""
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("unresolved-contact-sarah", "Sarah", dir_) == "unresolved-contact-sarah"


def test_non_contact_entity_id_unchanged():
    dir_ = _directory_with_blair()
    assert reconcile_contact_id("trip-bali-2026", "Bali", dir_) == "trip-bali-2026"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_reconcile.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement reconcile_contact_id**

```python
# packages/cyborg-server/cyborg_server/services/memory/reconcile.py
"""Map non-canonical contact IDs to canonical contact-{hex8} IDs via the contacts DB."""

from __future__ import annotations

import re

from cyborg_server.services.memory.contact_directory import ContactDirectory

_CANONICAL_RE = re.compile(r"^contact-[a-f0-9]{8}$")


def is_canonical_contact_id(entity_id: str) -> bool:
    return bool(_CANONICAL_RE.match(entity_id))


def reconcile_contact_id(
    entity_id: str,
    display_name: str,
    directory: ContactDirectory | None,
) -> str:
    """Return the canonical contact ID for *entity_id* if it can be resolved.

    If *entity_id* is already canonical, a non-contact entity, or cannot be
    matched against the contacts DB, it is returned unchanged.
    """
    if not entity_id:
        return entity_id
    if not entity_id.startswith("contact-") and not entity_id.startswith("unresolved-contact-"):
        return entity_id
    if _CANONICAL_RE.match(entity_id):
        return entity_id
    if directory is None or not display_name:
        return entity_id

    record = directory.get_by_name(display_name)
    if record is None:
        return entity_id
    return record.canonical_id
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_reconcile.py -v
```
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/cyborg-server/cyborg_server/services/memory/reconcile.py \
        packages/cyborg-server/tests/memory/test_reconcile.py
git commit -m "Add reconcile_contact_id helper for non-canonical IDs"
```

---

## Task 3: Heartbeat passes known_entities to bulletin generator

**Files:**
- Modify: `packages/cyborg-server/cyborg_server/heartbeat.py:168-208`
- Modify: `packages/cyborg-server/tests/test_heartbeat.py` (add test)

- [ ] **Step 1: Read the existing test to understand the fixture pattern**

```bash
cat /home/bob/cyborg/packages/cyborg-server/tests/test_heartbeat.py | head -60
```

- [ ] **Step 2: Write a failing test that asserts known_entities is populated**

```python
# Append to packages/cyborg-server/tests/test_heartbeat.py
import pytest
from unittest.mock import AsyncMock, patch

from cyborg_server import heartbeat


@pytest.mark.asyncio
async def test_heartbeat_passes_known_contacts_to_bulletin_generator(ctx, tmp_path, monkeypatch):
    """The bulletin generator must receive known_entities built from the contacts DB."""
    # Seed one contact
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, email, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("03f3902d-330b-4f15-bf2a-b1385a917677", "Blair Nicol",
         "+61401589328", "", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )

    captured = {}

    async def fake_generate(llm, gen_input):
        captured["known_entities"] = gen_input.known_entities
        return "---\ncreate_bulletin: false\nreason: test\nsession_id: x\n---\n"

    async def fake_validate(text):
        return True, {"create_bulletin": False}

    monkeypatch.setattr(
        "cyborg_server.services.memory.bulletin_generator.generate_bulletin",
        fake_generate,
    )
    monkeypatch.setattr(
        "cyborg_server.services.memory.bulletin_generator.validate_draft_bulletin",
        fake_validate,
    )

    # Run the bulletin block directly with a minimal fake session
    # ... (use real heartbeat._process_idle_sessions or extract a helper — see Step 3)
```

**Note:** The existing heartbeat code inlines the bulletin logic inside a loop. To make this testable, Step 3 extracts a helper `_generate_session_bulletin` and the test calls it directly.

- [ ] **Step 3: Refactor heartbeat to make the bulletin block testable**

In `heartbeat.py`, extract the inline block (current lines ~168-208) into a helper:

```python
# packages/cyborg-server/cyborg_server/heartbeat.py (add near top of file)
async def _generate_session_bulletin(
    ctx,
    session: dict,
    messages: list[dict],
    contact_to_name: dict[str, str],
) -> dict | None:
    """Generate a single bulletin for an idle session. Returns the gen result or None."""
    from cyborg_server.services.memory import MemoryService
    from cyborg_server.services.memory.bulletin_generator import (
        build_generator_input,
        generate_bulletin,
        validate_draft_bulletin,
    )
    from cyborg_server.services.memory.contact_directory import ContactDirectory
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    transcript_text = "\n".join(
        f"[{m.get('sender_id', m['role'])}] {m['content'][:500]}"
        for m in messages[-50:]
    )

    directory = await ContactDirectory.load(ctx.db)
    gen_input = build_generator_input(
        session_key=session["session_key"],
        transcript_start=session["active_from"],
        transcript_end=session["last_message_at"],
        transcript_text=transcript_text,
        contact_ids=list(contact_to_name.keys()),
        known_entities=directory.as_known_entities(),
    )
    llm = LLMDispatchService(ctx)
    draft = await generate_bulletin(llm, gen_input)
    is_valid, data = validate_draft_bulletin(draft)
    if not is_valid or not data.get("create_bulletin"):
        return None

    mem_svc = MemoryService(ctx)
    mem_svc.write_bulletin(
        ctx.settings.harness.workspace_dir,
        channel_id=gen_input.channel_id,
        source_type="session_transcript_range",
        source_id=session["session_key"],
        visibility=gen_input.visibility,
        scope=gen_input.scope,
        entities=data.get("entities", {}),
        content=draft,
    )
    return data
```

Replace the inline block in the loop with a call:

```python
await _generate_session_bulletin(ctx, session, messages, contact_to_name)
```

- [ ] **Step 4: Replace the test stub with a real assertion**

```python
@pytest.mark.asyncio
async def test_generate_session_bulletin_passes_known_contacts(ctx, monkeypatch):
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, email, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("03f3902d-330b-4f15-bf2a-b1385a917677", "Blair Nicol",
         "+61401589328", "", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )

    captured = {}

    async def fake_generate(llm, gen_input):
        captured["known_entities"] = gen_input.known_entities
        return (
            "---\n"
            "create_bulletin: false\n"
            "reason: test\n"
            "session_id: x\n"
            "---\n"
        )

    async def fake_validate(text):
        return True, {"create_bulletin": False, "reason": "test", "session_id": "x"}

    monkeypatch.setattr(
        "cyborg_server.heartbeat.generate_bulletin", fake_generate, raising=True
    )
    # If generate_bulletin is imported inside the function, also patch the module-level reference:
    import cyborg_server.services.memory.bulletin_generator as bg
    monkeypatch.setattr(bg, "generate_bulletin", fake_generate)
    monkeypatch.setattr(bg, "validate_draft_bulletin", fake_validate)

    session = {
        "session_key": "agent:main:whatsapp:dm:61401589328",
        "active_from": "2026-06-01T10:00:00",
        "last_message_at": "2026-06-01T10:05:00",
    }
    messages = [
        {"role": "user", "sender_id": "test", "content": "hello"},
        {"role": "assistant", "sender_id": "assistant", "content": "hi"},
    ]

    from cyborg_server import heartbeat
    await heartbeat._generate_session_bulletin(ctx, session, messages, {"test": "Blair"})

    assert "contacts" in captured["known_entities"]
    ids = [c["id"] for c in captured["known_entities"]["contacts"]]
    assert "contact-03f3902d" in ids
```

- [ ] **Step 5: Run the test**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/test_heartbeat.py::test_generate_session_bulletin_passes_known_contacts -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/cyborg-server/cyborg_server/heartbeat.py \
        packages/cyborg-server/tests/test_heartbeat.py
git commit -m "Pass known_entities from ContactDirectory to bulletin generator"
```

---

## Task 4: Strengthen BULLETIN_GENERATION_PROMPT — forbid ignoring known contact IDs

**Files:**
- Modify: `packages/cyborg-server/cyborg_server/services/memory/prompts.py:77-110`

No unit test — prompt is text. Verified by Task 5 integration.

- [ ] **Step 1: Replace the "Contact and Entity Rules" section**

In `prompts.py`, replace lines 77-110 (the section starting with `# Contact and Entity Rules`) with:

```python
# Contact and Entity Rules

Contacts must be referenced by canonical contact IDs only.

## Mandatory: use the provided known_entities.contacts list

A `known_entities.contacts` list is provided in the input. Each entry has the form:

  { id: contact-XXXXXXXX, display_name: "Full Name" }

RULES:

1. If a person mentioned in the transcript appears (by full name OR unambiguously
   by first name) in the known_entities.contacts list, you MUST use that entry's
   `id` verbatim. Do not invent a different ID for that person.

2. Do not invent `contact-{name-slug}` IDs (e.g. `contact-blair-nicol`,
   `contact-helen-burnside`) for contacts that appear in known_entities. These
   IDs break linkage to the contacts database.

3. Do not invent `contact-unknown-{name}` or `contact-X-first-name-only` IDs for
   contacts that appear in known_entities.

4. The `unresolved-contact-*` pattern is allowed ONLY for a person who is
   genuinely not in known_entities.contacts. If you previously generated an
   unresolved- ID for someone and now see them in known_entities, switch to
   the canonical id.

5. When uncertain whether a mentioned person matches a known contact, prefer
   the canonical ID over unresolved-. Use the matched_from field to record the
   transcript label:

   contacts:
     - id: contact-03f3902d
       matched_from: "Blair"
       resolution_status: resolved

## Non-contact entities

For non-contact entities (groups, channels, trips, locations, events, tasks,
artifacts, decisions), use known IDs if supplied. If no known ID exists, propose
a stable candidate ID using kebab case.

Examples:

trips:
  - id: trip-bali-2026
    label: "Bali 2026"
    resolution_status: proposed

All relationship references must use IDs, not names.
```

- [ ] **Step 2: Run existing memory tests to ensure no regressions**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/ -v
```
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add packages/cyborg-server/cyborg_server/services/memory/prompts.py
git commit -m "Strengthen bulletin prompt: forbid inventing IDs for known contacts"
```

---

## Task 5: Apply reconciliation in _update_entities_from_claims

**Files:**
- Modify: `packages/cyborg-server/cyborg_server/services/memory/service.py:336-363`
- Modify: `packages/cyborg-server/cyborg_server/services/memory/service.py` (also `extract_claims_from_bulletin` path — see step 2)
- Create: `packages/cyborg-server/tests/memory/test_entity_update_reconciliation.py`

- [ ] **Step 1: Write the failing test**

```python
# packages/cyborg-server/tests/memory/test_entity_update_reconciliation.py
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cyborg_server.services.memory.models import Claim, Bulletin
from cyborg_server.services.memory.service import MemoryService


@pytest.mark.asyncio
async def test_update_entities_reconciles_name_slug_to_canonical(ctx, tmp_path, monkeypatch):
    """A claim with subject_id contact-blair-nicol + display_name Blair Nicol
    gets written to contact-03f3902d.md, not contact-blair-nicol.md."""
    workspace = tmp_path
    memory_dir = workspace / "memory"
    (memory_dir / "entities" / "contact").mkdir(parents=True)

    # Seed the contact
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, email, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("03f3902d-330b-4f15-bf2a-b1385a917677", "Blair Nicol",
         "+61401589328", "", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )

    claims = [
        Claim(
            id="claim-test-001",
            type="fact",
            subject_id="contact-blair-nicol",
            predicate="likes_beer",
            object_id=None,
            status="active",
            source_bulletins=["bulletin-test"],
            visibility="private",
            scope=[],
            created_at=__import__("datetime").datetime.now(),
            superseded_by=[],
            body="Blair Nicol likes beer.",
        ),
    ]

    async def fake_chat(messages, **kw):
        # Return a write_entity op for whatever ID was passed in
        return (
            '[{"action":"write_entity",'
            '"entity_id":"contact-blair-nicol",'
            '"entity_type":"contact",'
            '"content":"---\\n'
            'entity_id: contact-blair-nicol\\n'
            'entity_type: contact\\n'
            'display_name: Blair Nicol\\n'
            'status: active\\n'
            '---\\n\\n'
            '# Blair Nicol\\n\\n'
            '## Summary\\n\\nLikes beer.\\n"}]'
        )

    llm = AsyncMock()
    llm.chat = fake_chat
    llm.memory_model = "test"

    svc = MemoryService(ctx)
    wrote = await svc._update_entities_from_claims(llm, memory_dir, claims)

    assert wrote == 1
    # The canonical entity should exist
    assert (memory_dir / "entities" / "contact" / "contact-03f3902d.md").is_file()
    # The non-canonical one should NOT
    assert not (memory_dir / "entities" / "contact" / "contact-blair-nicol.md").is_file()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_entity_update_reconciliation.py -v
```
Expected: FAIL — entity written to `contact-blair-nicol.md` instead of canonical.

- [ ] **Step 3: Modify `_update_entities_from_claims` to apply reconciliation**

In `service.py`, replace `_update_entities_from_claims` (lines 336-363) with:

```python
async def _update_entities_from_claims(
    self, llm: Any, memory_dir: Path, claims: list[Claim]
) -> int:
    """Use LLM to update entity documents from claims — one call per entity.

    Before grouping, claims with non-canonical contact subject_ids are
    reconciled against the contacts DB so that contact-blair-nicol,
    unresolved-contact-blair, etc. all merge into the canonical
    contact-{hex8} entity when a DB match exists.
    """
    if not claims:
        return 0

    from collections import defaultdict
    from cyborg_server.services.memory.contact_directory import ContactDirectory
    from cyborg_server.services.memory.reconcile import reconcile_contact_id

    directory = None
    if self.ctx and hasattr(self.ctx, "db") and self.ctx.db:
        directory = await ContactDirectory.load(self.ctx.db)

    # Pre-compute display_name lookup from existing entity files so we can
    # reconcile even when the claim doesn't carry a display_name itself.
    existing_name_map = self._index_contact_display_names(memory_dir)

    claims_by_entity: dict[str, list[Claim]] = defaultdict(list)
    for c in claims:
        if not (c.subject_id and isinstance(c.subject_id, str)):
            continue
        sid = normalize_entity_id(c.subject_id)
        display_name = existing_name_map.get(sid, "")
        canonical = reconcile_contact_id(sid, display_name, directory)
        if canonical != sid and isinstance(c.object_id, str):
            # Also reconcile object_id if it's a contact
            c.object_id = reconcile_contact_id(
                normalize_entity_id(c.object_id),
                existing_name_map.get(normalize_entity_id(c.object_id), ""),
                directory,
            )
        c.subject_id = canonical
        claims_by_entity[canonical].append(c)

    all_existing_ids = self._list_all_entity_ids(memory_dir)
    contact_ids = {eid for eid in claims_by_entity if eid.startswith("contact-")}
    contact_name_map = await self._lookup_contact_names(contact_ids) if contact_ids else {}

    wrote = 0
    for entity_id, entity_claims in claims_by_entity.items():
        wrote += await self._update_single_entity(
            llm, memory_dir, entity_id, entity_claims,
            all_existing_ids=all_existing_ids,
            contact_name_map=contact_name_map,
        )
    return wrote


def _index_contact_display_names(self, memory_dir: Path) -> dict[str, str]:
    """Return {entity_id: display_name} for every contact entity on disk."""
    contact_dir = memory_dir / "entities" / "contact"
    if not contact_dir.is_dir():
        return {}
    out: dict[str, str] = {}
    for md_file in contact_dir.glob("*.md"):
        try:
            fm, _ = parse_frontmatter(md_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if fm.get("display_name"):
            out[md_file.stem] = fm["display_name"]
    return out
```

Also update `Claim` import behavior — since we're mutating `c.subject_id` and `c.object_id` in place, callers that re-use the claim list afterwards will see the canonicalized versions. That's intended.

- [ ] **Step 4: Update `_update_single_entity` to write `contact_id`/`email`/`phone_number`**

In `service.py`, locate the `_update_single_entity` body where it writes the file (around line 467). After `path.write_text(content, ...)`, add a step that, if the entity is a contact and the canonical_id matches a DB row, rewrites the frontmatter to include the FK fields:

```python
# After path.write_text(content, encoding="utf-8") (first branch, around line 468):
self._enrich_contact_frontmatter(path, entity_id=normalize_entity_id(eid))
```

And after the second `path.write_text(...)` (around line 493):

```python
self._enrich_contact_frontmatter(path, entity_id=entity.entity_id)
```

Add the helper method:

```python
def _enrich_contact_frontmatter(self, path: Path, entity_id: str) -> None:
    """If entity_id is a canonical contact-{hex8}, add contact_id/email/phone."""
    if not entity_id.startswith("contact-") or self.ctx is None:
        return
    import re
    if not re.match(r"^contact-[a-f0-9]{8}$", entity_id):
        return
    try:
        from cyborg_server.services.memory.contact_directory import ContactDirectory
    except Exception:
        return
    # Synchronous wrapper — call from async context preferred, but for
    # post-write enrichment we re-read+rewrite, so we use the cached directory
    # on the instance if available, else skip (cleanup pass will catch up).
    cache = getattr(self, "_contact_dir_cache", None)
    if cache is None:
        return  # cleanup pass handles
    record = cache.get_by_canonical_id(entity_id)
    if record is None:
        return
    raw = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(raw)
    fm["contact_id"] = record.uuid
    if record.email:
        fm["email"] = record.email
    if record.phone_number:
        fm["phone_number"] = record.phone_number
    path.write_text(serialize_frontmatter(fm, body), encoding="utf-8")
```

Also, ensure the directory is loaded once and cached during `_update_entities_from_claims`:

```python
# In _update_entities_from_claims, after loading directory:
self._contact_dir_cache = directory
```

- [ ] **Step 5: Run the test**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_entity_update_reconciliation.py -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/cyborg-server/cyborg_server/services/memory/service.py \
        packages/cyborg-server/tests/memory/test_entity_update_reconciliation.py
git commit -m "Reconcile non-canonical contact IDs to canonical in entity update"
```

---

## Task 6: merge_entity_docs — combine duplicate entities

**Files:**
- Create: `packages/cyborg-server/cyborg_server/services/memory/cleanup.py` (partial — add `merge_entity_docs` only)
- Create: `packages/cyborg-server/tests/memory/test_cleanup.py` (partial — add the merge tests)

- [ ] **Step 1: Write failing tests for merge_entity_docs**

```python
# packages/cyborg-server/tests/memory/test_cleanup.py
from __future__ import annotations

from pathlib import Path

import pytest

from cyborg_server.services.memory.cleanup import merge_entity_docs
from cyborg_server.services.memory.models import EntityDocument


def test_merge_combines_timelines_and_source_bulletins():
    canonical = EntityDocument(
        entity_id="contact-03f3902d",
        entity_type="contact",
        display_name="Blair Nicol",
        status="active",
        extra_frontmatter={},
        body=(
            "## Summary\n\nLikes beer.\n\n"
            "## Timeline\n\n- 2026-05-30: Asked about beer.\n\n"
            "## Source Bulletins\n\n- bulletin-aaa\n"
        ),
    )
    dup = EntityDocument(
        entity_id="contact-blair-nicol",
        entity_type="contact",
        display_name="Blair Nicol",
        status="active",
        extra_frontmatter={},
        body=(
            "## Summary\n\nFrench, married to Katie.\n\n"
            "## Timeline\n\n- 2026-05-31: Mentioned being French.\n\n"
            "## Source Bulletins\n\n- bulletin-bbb\n"
        ),
    )
    merged = merge_entity_docs(canonical, dup)
    assert merged.entity_id == "contact-03f3902d"
    assert "Likes beer." in merged.body
    assert "French, married to Katie." in merged.body
    assert "2026-05-30: Asked about beer." in merged.body
    assert "2026-05-31: Mentioned being French." in merged.body
    assert "bulletin-aaa" in merged.body
    assert "bulletin-bbb" in merged.body


def test_merge_preserves_canonical_display_name():
    canonical = EntityDocument(
        entity_id="contact-fa10577d", entity_type="contact",
        display_name="Gareth Parry", status="active", extra_frontmatter={},
        body="## Summary\n\nA.\n",
    )
    dup = EntityDocument(
        entity_id="unresolved-contact-gareth", entity_type="contact",
        display_name="Gareth", status="active", extra_frontmatter={},
        body="## Summary\n\nB.\n",
    )
    merged = merge_entity_docs(canonical, dup)
    assert merged.display_name == "Gareth Parry"


def test_merge_related_entities_deduped():
    canonical = EntityDocument(
        entity_id="contact-03f3902d", entity_type="contact",
        display_name="Blair Nicol", status="active", extra_frontmatter={},
        body=(
            "## Related Entities\n\n"
            "contacts: []\ngroups: []\nchannels:\n  - channel-x\n"
            "trips: []\nlocations: []\nevents: []\ntasks: []\nartifacts: []\ndecisions: []\n"
        ),
    )
    dup = EntityDocument(
        entity_id="contact-blair-nicol", entity_type="contact",
        display_name="Blair Nicol", status="active", extra_frontmatter={},
        body=(
            "## Related Entities\n\n"
            "contacts:\n  - contact-mike\n"
            "groups: []\nchannels:\n  - channel-x\n  - channel-y\n"
            "trips: []\nlocations: []\nevents: []\ntasks: []\nartifacts: []\ndecisions: []\n"
        ),
    )
    merged = merge_entity_docs(canonical, dup)
    # Both channel-x and channel-y should appear, but channel-x should not duplicate
    assert merged.body.count("channel-x") == 1
    assert "channel-y" in merged.body
    assert "contact-mike" in merged.body
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_cleanup.py -v
```
Expected: ImportError.

- [ ] **Step 3: Implement merge_entity_docs**

```python
# packages/cyborg-server/cyborg_server/services/memory/cleanup.py
"""One-shot cleanup of duplicate contact entity documents."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from cyborg_server.services.memory.models import (
    ENTITY_CATEGORIES,
    EntityDocument,
    parse_frontmatter,
    serialize_frontmatter,
)

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _extract_section(body: str, heading: str) -> str:
    """Return the contents under `## heading`, or empty string if missing."""
    pat = re.compile(rf"^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
    m = pat.search(body)
    return m.group(1).strip() if m else ""


def _parse_related_entities(section_body: str) -> dict[str, list[str]]:
    """Parse a Related Entities section into {category: [ids]}."""
    out: dict[str, list[str]] = {cat.rstrip("s"): [] for cat in ENTITY_CATEGORIES}
    current = None
    for line in section_body.splitlines():
        line = line.rstrip()
        if not line:
            continue
        m = re.match(r"^(\w+):\s*$", line)
        if m:
            current = m.group(1)
            continue
        if current and line.lstrip().startswith("-"):
            item = line.lstrip("- ").strip()
            if item and item != "[]":
                out.setdefault(current, []).append(item)
    return out


def _serialize_related_entities(related: dict[str, list[str]]) -> str:
    cats = ["contacts", "groups", "channels", "trips", "locations",
            "events", "tasks", "artifacts", "decisions"]
    lines = ["## Related Entities", ""]
    for cat in cats:
        key = cat.rstrip("s")
        items = sorted(set(related.get(key, [])))
        if items:
            lines.append(f"{cat}:")
            for item in items:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{cat}: []")
    return "\n".join(lines) + "\n"


def merge_entity_docs(canonical: EntityDocument, duplicate: EntityDocument) -> EntityDocument:
    """Merge *duplicate* into *canonical*, returning the merged document.

    Sections combined: Summary, Current State, Timeline, Source Bulletins,
    Related Entities. The canonical entity_id and display_name win on conflict.
    """
    # Combine simple sections — keep both summaries lines, dedupe
    def combine_text(a: str, b: str) -> str:
        a_lines = [ln.strip() for ln in a.splitlines() if ln.strip()]
        b_lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        seen: set[str] = set()
        out: list[str] = []
        for ln in a_lines + b_lines:
            if ln not in seen:
                seen.add(ln)
                out.append(ln)
        return "\n".join(out)

    sum_a = _extract_section(canonical.body, "Summary")
    sum_b = _extract_section(duplicate.body, "Summary")
    state_a = _extract_section(canonical.body, "Current State")
    state_b = _extract_section(duplicate.body, "Current State")
    timeline_a = _extract_section(canonical.body, "Timeline")
    timeline_b = _extract_section(duplicate.body, "Timeline")
    sources_a = _extract_section(canonical.body, "Source Bulletins")
    sources_b = _extract_section(duplicate.body, "Source Bulletins")

    rel_a = _parse_related_entities(_extract_section(canonical.body, "Related Entities"))
    rel_b = _parse_related_entities(_extract_section(duplicate.body, "Related Entities"))
    merged_rel: dict[str, list[str]] = {}
    for key in set(rel_a.keys()) | set(rel_b.keys()):
        merged_rel[key] = rel_a.get(key, []) + rel_b.get(key, [])

    sections = ["## Summary", "", combine_text(sum_a, sum_b), ""]
    if state_a or state_b:
        sections += ["## Current State", "", combine_text(state_a, state_b), ""]
    sections += [
        _serialize_related_entities(merged_rel),
        "",
        "## Timeline", "", combine_text(timeline_a, timeline_b), "",
        "## Source Bulletins", "", combine_text(sources_a, sources_b),
    ]

    return EntityDocument(
        entity_id=canonical.entity_id,
        entity_type=canonical.entity_type,
        display_name=canonical.display_name or duplicate.display_name,
        status=canonical.status,
        extra_frontmatter={**duplicate.extra_frontmatter, **canonical.extra_frontmatter},
        body="\n".join(sections) + "\n",
    )
```

- [ ] **Step 4: Run the tests**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_cleanup.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/cyborg-server/cyborg_server/services/memory/cleanup.py \
        packages/cyborg-server/tests/memory/test_cleanup.py
git commit -m "Add merge_entity_docs for combining duplicate entities"
```

---

## Task 7: build_renaming_map — match non-canonical contacts to canonical

**Files:**
- Modify: `packages/cyborg-server/cyborg_server/services/memory/cleanup.py`
- Modify: `packages/cyborg-server/tests/memory/test_cleanup.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to packages/cyborg-server/tests/memory/test_cleanup.py
import pytest
from cyborg_server.services.memory.cleanup import build_renaming_map
from cyborg_server.services.memory.contact_directory import ContactDirectory, ContactRecord


def _write_contact_entity(memory_dir: Path, entity_id: str, display_name: str) -> None:
    p = memory_dir / "entities" / "contact" / f"{entity_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\n"
        f"entity_id: {entity_id}\n"
        "entity_type: contact\n"
        f"display_name: {display_name}\n"
        "status: active\n"
        "---\n\n"
        f"# {display_name}\n",
        encoding="utf-8",
    )


def test_renaming_map_matches_name_slug_to_canonical(tmp_path):
    memory_dir = tmp_path / "memory"
    _write_contact_entity(memory_dir, "contact-03f3902d", "Blair Nicol")
    _write_contact_entity(memory_dir, "contact-blair-nicol", "Blair Nicol")
    _write_contact_entity(memory_dir, "unresolved-contact-gareth", "Gareth")

    directory = ContactDirectory([
        ContactRecord(
            uuid="03f3902d-330b-4f15-bf2a-b1385a917677",
            canonical_id="contact-03f3902d",
            name="Blair Nicol",
            phone_number="+61401589328",
            email="",
        ),
    ])

    rename, merge_into = build_renaming_map(memory_dir, directory)

    # contact-blair-nicol should be renamed to contact-03f3902d
    assert rename.get("contact-blair-nicol") == "contact-03f3902d"
    # And the duplicate needs to be merged into the canonical
    assert merge_into.get("contact-blair-nicol") == "contact-03f3902d"
    # Gareth is not in the directory, so it stays as-is
    assert "unresolved-contact-gareth" not in rename


def test_renaming_map_handles_two_orphans_with_same_name(tmp_path):
    memory_dir = tmp_path / "memory"
    _write_contact_entity(memory_dir, "bob-sr", "Bob Sr.")
    _write_contact_entity(memory_dir, "contact-bob-sr", "Bob Sr.")

    rename, merge_into = build_renaming_map(memory_dir, directory=None)

    # Both are non-canonical; pick contact-bob-sr as the winner (contact- prefix wins)
    # and merge the other into it.
    assert rename.get("bob-sr") == "contact-bob-sr"
    assert merge_into.get("bob-sr") == "contact-bob-sr"
```

- [ ] **Step 2: Run the test**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_cleanup.py::test_renaming_map_matches_name_slug_to_canonical tests/memory/test_cleanup.py::test_renaming_map_handles_two_orphans_with_same_name -v
```
Expected: ImportError on `build_renaming_map`.

- [ ] **Step 3: Implement build_renaming_map**

Add to `cleanup.py`:

```python
import re
from cyborg_server.services.memory.contact_directory import ContactDirectory
from cyborg_server.services.memory.reconcile import is_canonical_contact_id

_NON_CANONICAL_CONTACT_RE = re.compile(
    r"^(contact-(?!([a-f0-9]{8})$).*|unresolved-contact-.*)$"
)


def build_renaming_map(
    memory_dir: Path,
    directory: ContactDirectory | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Scan contact entities and compute (rename, merge_into) maps.

    - rename: {old_id: new_id} — every reference to old_id should be rewritten to new_id
    - merge_into: {dup_id: canonical_id} — dup_id's body should be merged into
      canonical_id before deletion

    For non-canonical IDs that match a DB contact by display_name, the new_id
    is the canonical contact-{hex8}. For orphan duplicates that share a
    display_name with no DB match, the lexicographically smallest `contact-`-
    prefixed ID wins (or the smallest overall if neither has the prefix).
    """
    contact_dir = memory_dir / "entities" / "contact"
    if not contact_dir.is_dir():
        return {}, {}

    # Collect (entity_id, display_name)
    rows: list[tuple[str, str]] = []
    for md_file in sorted(contact_dir.glob("*.md")):
        fm, _ = parse_frontmatter(md_file.read_text(encoding="utf-8"))
        rows.append((md_file.stem, fm.get("display_name", "")))

    # Step 1: non-canonical → canonical via DB lookup
    rename: dict[str, str] = {}
    for entity_id, name in rows:
        if is_canonical_contact_id(entity_id):
            continue
        if directory is None or not name:
            continue
        record = directory.get_by_name(name)
        if record is not None:
            rename[entity_id] = record.canonical_id

    # Step 2: orphan duplicates (same display_name) — pick a winner
    by_name: dict[str, list[str]] = {}
    for entity_id, name in rows:
        if not name:
            continue
        # Skip IDs already being renamed in step 1
        if entity_id in rename:
            continue
        by_name.setdefault(name, []).append(entity_id)

    for name, ids in by_name.items():
        if len(ids) < 2:
            continue
        # Prefer contact- prefix, then lexicographic
        def sort_key(eid: str) -> tuple[int, str]:
            return (0 if eid.startswith("contact-") else 1, eid)
        ids_sorted = sorted(ids, key=sort_key)
        winner = ids_sorted[0]
        for loser in ids_sorted[1:]:
            rename[loser] = winner

    # Step 3: compute merge_into — only the entries where the destination
    # currently exists on disk (so we need to merge bodies before deleting).
    existing_ids = {eid for eid, _ in rows}
    merge_into: dict[str, str] = {}
    for old, new in rename.items():
        if new in existing_ids:
            merge_into[old] = new

    return rename, merge_into
```

- [ ] **Step 4: Run the tests**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_cleanup.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/cyborg-server/cyborg_server/services/memory/cleanup.py \
        packages/cyborg-server/tests/memory/test_cleanup.py
git commit -m "Add build_renaming_map for duplicate contact entities"
```

---

## Task 8: Rewrite claim files and bulletin entity references

**Files:**
- Modify: `packages/cyborg-server/cyborg_server/services/memory/cleanup.py`
- Modify: `packages/cyborg-server/tests/memory/test_cleanup.py`

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/memory/test_cleanup.py
from cyborg_server.services.memory.cleanup import (
    rewrite_claims,
    rewrite_bulletin_entities,
)


def _write_claim(memory_dir: Path, claim_id: str, subject_id: str, object_id: str | None) -> None:
    p = memory_dir / "claims" / f"{claim_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = {"id": claim_id, "type": "fact", "subject_id": subject_id,
          "predicate": "x", "object_id": object_id, "status": "active",
          "source_bulletins": [], "visibility": "private", "scope": []}
    p.write_text(serialize_frontmatter(fm, "# Claim\n\nbody"), encoding="utf-8")


def test_rewrite_claims_renames_subject_and_object_ids(tmp_path):
    memory_dir = tmp_path / "memory"
    _write_claim(memory_dir, "claim-001", "contact-blair-nicol", None)
    _write_claim(memory_dir, "claim-002", "contact-mike", "contact-blair-nicol")
    _write_claim(memory_dir, "claim-003", "trip-bali", "location-x")

    rename = {"contact-blair-nicol": "contact-03f3902d",
              "contact-mike": "contact-7c9f0fd7"}
    count = rewrite_claims(memory_dir, rename)

    assert count == 2  # claim-001 and claim-002 changed
    from cyborg_server.services.memory.models import parse_frontmatter
    c1 = parse_frontmatter((memory_dir / "claims" / "claim-001.md").read_text())[0]
    assert c1["subject_id"] == "contact-03f3902d"
    c2 = parse_frontmatter((memory_dir / "claims" / "claim-002.md").read_text())[0]
    assert c2["subject_id"] == "contact-7c9f0fd7"
    assert c2["object_id"] == "contact-03f3902d"
    c3 = parse_frontmatter((memory_dir / "claims" / "claim-003.md").read_text())[0]
    assert c3["subject_id"] == "trip-bali"  # untouched


def _write_bulletin(memory_dir: Path, bid: str, contact_refs: list[dict]) -> None:
    p = memory_dir / "bulletins" / "2026" / "06" / f"{bid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = {"id": bid, "entities": {"contacts": contact_refs,
            "groups": [], "channels": [], "trips": [], "locations": [],
            "events": [], "tasks": [], "artifacts": [], "decisions": []}}
    p.write_text(serialize_frontmatter(fm, "# Update\n\nbody"), encoding="utf-8")


def test_rewrite_bulletin_entities_renames_contact_refs(tmp_path):
    memory_dir = tmp_path / "memory"
    _write_bulletin(memory_dir, "b-1", [
        {"id": "contact-blair-nicol", "matched_from": "Blair"},
        {"id": "contact-03f3902d"},
    ])
    _write_bulletin(memory_dir, "b-2", [{"id": "contact-mike"}])

    rename = {"contact-blair-nicol": "contact-03f3902d",
              "contact-mike": "contact-7c9f0fd7"}
    count = rewrite_bulletin_entities(memory_dir, rename)

    assert count == 2
    from cyborg_server.services.memory.models import parse_frontmatter
    b1 = parse_frontmatter((memory_dir / "bulletins" / "2026" / "06" / "b-1.md").read_text())[0]
    contact_ids = [c["id"] for c in b1["entities"]["contacts"]]
    # The original canonical entry should not be duplicated
    assert contact_ids.count("contact-03f3902d") == 1
    b2 = parse_frontmatter((memory_dir / "bulletins" / "2026" / "06" / "b-2.md").read_text())[0]
    assert b2["entities"]["contacts"][0]["id"] == "contact-7c9f0fd7"
```

- [ ] **Step 2: Run the tests**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_cleanup.py -v
```
Expected: ImportError on `rewrite_claims`, `rewrite_bulletin_entities`.

- [ ] **Step 3: Implement rewrite_claims and rewrite_bulletin_entities**

Add to `cleanup.py`:

```python
def _rewrite_refs(value: str, rename: dict[str, str]) -> str:
    return rename.get(value, value)


def rewrite_claims(memory_dir: Path, rename: dict[str, str]) -> int:
    """Rewrite subject_id/object_id in every claim file. Returns changed count."""
    claims_dir = memory_dir / "claims"
    if not claims_dir.is_dir():
        return 0
    changed = 0
    for md_file in claims_dir.glob("*.md"):
        raw = md_file.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)
        new_subj = _rewrite_refs(fm.get("subject_id", ""), rename)
        new_obj = fm.get("object_id")
        if isinstance(new_obj, str):
            new_obj = _rewrite_refs(new_obj, rename)
        if new_subj != fm.get("subject_id") or new_obj != fm.get("object_id"):
            fm["subject_id"] = new_subj
            fm["object_id"] = new_obj
            md_file.write_text(serialize_frontmatter(fm, body), encoding="utf-8")
            changed += 1
    return changed


def rewrite_bulletin_entities(memory_dir: Path, rename: dict[str, str]) -> int:
    """Rewrite entities.contacts[].id in every bulletin. Dedupe within each bulletin."""
    bulletins_dir = memory_dir / "bulletins"
    if not bulletins_dir.is_dir():
        return 0
    changed = 0
    for md_file in bulletins_dir.rglob("*.md"):
        raw = md_file.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)
        entities = fm.get("entities") or {}
        contacts = entities.get("contacts") or []
        if not isinstance(contacts, list):
            continue
        new_contacts: list[dict] = []
        seen: set[str] = set()
        local_changed = False
        for entry in contacts:
            if isinstance(entry, str):
                entry = {"id": entry}
            old_id = entry.get("id", "")
            new_id = _rewrite_refs(old_id, rename)
            if new_id != old_id:
                local_changed = True
            if new_id in seen:
                local_changed = True
                continue
            seen.add(new_id)
            new_entry = dict(entry)
            new_entry["id"] = new_id
            new_contacts.append(new_entry)
        if local_changed:
            entities["contacts"] = new_contacts
            fm["entities"] = entities
            md_file.write_text(serialize_frontmatter(fm, body), encoding="utf-8")
            changed += 1
    return changed


def rewrite_entity_related(memory_dir: Path, rename: dict[str, str]) -> int:
    """Rewrite Related Entities contact refs in every entity document."""
    entities_dir = memory_dir / "entities"
    if not entities_dir.is_dir():
        return 0
    changed = 0
    for type_dir in entities_dir.iterdir():
        if not type_dir.is_dir():
            continue
        for md_file in type_dir.glob("*.md"):
            raw = md_file.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(raw)
            new_body = body
            for old, new in rename.items():
                # only match as a bullet item to avoid partial replacements
                new_body = re.sub(
                    rf"(\s*-\s+){re.escape(old)}(\s*)$",
                    rf"\g<1>{new}\g<2>",
                    new_body,
                    flags=re.MULTILINE,
                )
            if new_body != body:
                md_file.write_text(serialize_frontmatter(fm, new_body), encoding="utf-8")
                changed += 1
    return changed
```

- [ ] **Step 4: Run the tests**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_cleanup.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/cyborg-server/cyborg_server/services/memory/cleanup.py \
        packages/cyborg-server/tests/memory/test_cleanup.py
git commit -m "Add rewrite helpers for claims, bulletins, entity related entities"
```

---

## Task 9: run_cleanup — orchestrate the full pass

**Files:**
- Modify: `packages/cyborg-server/cyborg_server/services/memory/cleanup.py`
- Modify: `packages/cyborg-server/tests/memory/test_cleanup.py`

- [ ] **Step 1: Write the failing integration test**

```python
# Append to tests/memory/test_cleanup.py
from cyborg_server.services.memory.cleanup import run_cleanup
from cyborg_server.services.memory.contact_directory import ContactDirectory, ContactRecord


@pytest.mark.asyncio
async def test_run_cleanup_end_to_end(tmp_path):
    memory_dir = tmp_path / "memory"
    # Three entities: canonical Blair, name-slug Blair, orphan Bob Sr x2
    _write_contact_entity(memory_dir, "contact-03f3902d", "Blair Nicol")
    _write_contact_entity(memory_dir, "contact-blair-nicol", "Blair Nicol")
    _write_contact_entity(memory_dir, "bob-sr", "Bob Sr.")
    _write_contact_entity(memory_dir, "contact-bob-sr", "Bob Sr.")
    # A claim and bulletin referencing the doomed IDs
    _write_claim(memory_dir, "claim-001", "contact-blair-nicol", "bob-sr")
    _write_bulletin(memory_dir, "b-1", [{"id": "contact-blair-nicol"}])

    directory = ContactDirectory([
        ContactRecord("03f3902d-330b-4f15-bf2a-b1385a917677", "contact-03f3902d",
                      "Blair Nicol", "+61401589328", ""),
    ])

    summary = await run_cleanup(memory_dir, directory, dry_run=False)

    # Canonical Blair still exists, slug Blair deleted
    assert (memory_dir / "entities" / "contact" / "contact-03f3902d.md").is_file()
    assert not (memory_dir / "entities" / "contact" / "contact-blair-nicol.md").is_file()
    # Bob Sr. orphan: contact-bob-sr wins, bob-sr deleted
    assert (memory_dir / "entities" / "contact" / "contact-bob-sr.md").is_file()
    assert not (memory_dir / "entities" / "contact" / "bob-sr.md").is_file()
    # Claim rewritten
    from cyborg_server.services.memory.models import parse_frontmatter
    c1 = parse_frontmatter((memory_dir / "claims" / "claim-001.md").read_text())[0]
    assert c1["subject_id"] == "contact-03f3902d"
    assert c1["object_id"] == "contact-bob-sr"
    # Bulletin rewritten
    b1 = parse_frontmatter((memory_dir / "bulletins" / "2026" / "06" / "b-1.md").read_text())[0]
    assert b1["entities"]["contacts"][0]["id"] == "contact-03f3902d"
    # Contact FK written into canonical entity
    canon = parse_frontmatter(
        (memory_dir / "entities" / "contact" / "contact-03f3902d.md").read_text()
    )[0]
    assert canon["contact_id"] == "03f3902d-330b-4f15-bf2a-b1385a917677"
    assert canon["phone_number"] == "+61401589328"

    assert summary["merged"] >= 2
    assert summary["rewritten_claims"] >= 1
    assert summary["rewritten_bulletins"] >= 1
```

- [ ] **Step 2: Run the test**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_cleanup.py::test_run_cleanup_end_to_end -v
```
Expected: ImportError on `run_cleanup`.

- [ ] **Step 3: Implement run_cleanup**

Add to `cleanup.py`:

```python
async def run_cleanup(
    memory_dir: Path,
    directory: ContactDirectory | None,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """End-to-end cleanup of contact entity duplicates.

    Steps:
      1. Build renaming map
      2. Merge duplicate entity bodies into canonical entity files
      3. Delete the duplicate files
      4. Rewrite every claim, bulletin, and entity Related Entities section
      5. Enrich canonical contact entity frontmatter with contact_id/email/phone
    """
    rename, merge_into = build_renaming_map(memory_dir, directory)

    merged = 0
    deleted = 0
    contact_dir = memory_dir / "entities" / "contact"

    # Step 2 + 3: merge bodies, then delete
    for dup_id, canon_id in merge_into.items():
        if dry_run:
            continue
        dup_path = contact_dir / f"{dup_id}.md"
        canon_path = contact_dir / f"{canon_id}.md"
        if not dup_path.is_file() or not canon_path.is_file():
            continue
        dup_doc = _read_entity_doc(dup_path)
        canon_doc = _read_entity_doc(canon_path)
        if dup_doc and canon_doc:
            merged_doc = merge_entity_docs(canon_doc, dup_doc)
            canon_path.write_text(
                serialize_frontmatter(
                    {
                        "entity_id": merged_doc.entity_id,
                        "entity_type": merged_doc.entity_type,
                        "display_name": merged_doc.display_name,
                        "status": merged_doc.status,
                        **merged_doc.extra_frontmatter,
                    },
                    merged_doc.body,
                ),
                encoding="utf-8",
            )
            merged += 1
        dup_path.unlink()
        deleted += 1

    # Pure renames (no on-disk canonical target) — just rename the file
    for old, new in rename.items():
        if old in merge_into:
            continue
        if dry_run:
            continue
        old_path = contact_dir / f"{old}.md"
        new_path = contact_dir / f"{new}.md"
        if old_path.is_file() and not new_path.is_file():
            raw = old_path.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(raw)
            fm["entity_id"] = new
            new_path.write_text(serialize_frontmatter(fm, body), encoding="utf-8")
            old_path.unlink()
            deleted += 1

    # Step 4: rewrite refs
    rewritten_claims = rewrite_claims(memory_dir, rename) if not dry_run else 0
    rewritten_bulletins = rewrite_bulletin_entities(memory_dir, rename) if not dry_run else 0
    rewritten_related = rewrite_entity_related(memory_dir, rename) if not dry_run else 0

    # Step 5: enrich canonical entities with FK
    enriched = 0
    if directory is not None and not dry_run:
        for md_file in contact_dir.glob("*.md"):
            record = directory.get_by_canonical_id(md_file.stem)
            if record is None:
                continue
            raw = md_file.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(raw)
            fm["contact_id"] = record.uuid
            if record.email:
                fm["email"] = record.email
            if record.phone_number:
                fm["phone_number"] = record.phone_number
            md_file.write_text(serialize_frontmatter(fm, body), encoding="utf-8")
            enriched += 1

    return {
        "renamed": len(rename),
        "merged": merged,
        "deleted": deleted,
        "rewritten_claims": rewritten_claims,
        "rewritten_bulletins": rewritten_bulletins,
        "rewritten_related": rewritten_related,
        "enriched": enriched,
    }


def _read_entity_doc(path: Path) -> EntityDocument | None:
    if not path.is_file():
        return None
    fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    return EntityDocument(
        entity_id=fm.get("entity_id", path.stem),
        entity_type=fm.get("entity_type", ""),
        display_name=fm.get("display_name", ""),
        status=fm.get("status", "active"),
        extra_frontmatter={
            k: v for k, v in fm.items()
            if k not in {"entity_id", "entity_type", "display_name", "status"}
        },
        body=body,
    )
```

- [ ] **Step 4: Run the tests**

```bash
cd /home/bob/cyborg/packages/cyborg-server && pytest tests/memory/test_cleanup.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add packages/cyborg-server/cyborg_server/services/memory/cleanup.py \
        packages/cyborg-server/tests/memory/test_cleanup.py
git commit -m "Add run_cleanup orchestrator + contact FK enrichment"
```

---

## Task 10: Wire cleanup into CLI — `cyborg memory cleanup-contacts`

**Files:**
- Modify: `packages/cyborg-server/cyborg_server/cli.py` (add new command after `memory_validate` ~line 1920)

- [ ] **Step 1: Add the CLI command**

Insert after the `memory_validate` block (find it with `grep -n "memory_validate" cli.py`):

```python
@memory_app.command("cleanup-contacts")
def memory_cleanup_contacts(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change without writing")] = False,
) -> None:
    """Remove duplicate contact entities and rewire references to canonical IDs."""
    import asyncio
    asyncio.run(_memory_cleanup_contacts(dry_run))


async def _memory_cleanup_contacts(dry_run: bool) -> None:
    from cyborg_server.config import Settings
    from cyborg_server.context import AppContext
    from cyborg_server.database import Database
    from cyborg_server.services.memory.cleanup import run_cleanup
    from cyborg_server.services.memory.contact_directory import ContactDirectory

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent / "schemas"
    db_path = settings.db_path or Path("cyborg.db")
    db = Database(db_path, schema_dir)
    await db.connect()
    ctx = AppContext(settings=settings, db=db)

    try:
        workspace = settings.harness.workspace_dir
        memory_dir = workspace / "memory"
        directory = await ContactDirectory.load(db)

        typer.echo(f"Loaded {len(directory.all_canonical_ids())} contacts from DB")
        if dry_run:
            from cyborg_server.services.memory.cleanup import build_renaming_map
            rename, merge = build_renaming_map(memory_dir, directory)
            typer.echo(f"\n[Dry run] Would rename {len(rename)} entities")
            typer.echo(f"[Dry run] Would merge {len(merge)} duplicates into canonical entities")
            for old, new in sorted(rename.items()):
                typer.echo(f"  {old} -> {new}")
            return

        result = await run_cleanup(memory_dir, directory, dry_run=False)
        typer.echo(f"\nCleanup result:")
        typer.echo(f"  Renamed: {result['renamed']}")
        typer.echo(f"  Merged:  {result['merged']}")
        typer.echo(f"  Deleted: {result['deleted']}")
        typer.echo(f"  Rewritten claims:     {result['rewritten_claims']}")
        typer.echo(f"  Rewritten bulletins:  {result['rewritten_bulletins']}")
        typer.echo(f"  Rewritten related:    {result['rewritten_related']}")
        typer.echo(f"  Enriched with DB FK:  {result['enriched']}")
    finally:
        await db.close()
```

- [ ] **Step 2: Smoke test — `--dry-run` on the live data**

```bash
cd /home/bob/cyborg && python -m cyborg_server.cli memory cleanup-contacts --dry-run 2>&1 | head -40
```
Expected: lists the duplicates discovered above (`contact-blair-nicol → contact-03f3902d`, `bob-sr → contact-bob-sr`, etc.) without writing anything.

- [ ] **Step 3: Verify dry-run did not modify files**

```bash
ls -la /home/bob/.config/cyborg/harness/memory/entities/contact/ | wc -l
```
Compare to the count before running — should be unchanged.

- [ ] **Step 4: Run the real cleanup**

```bash
cd /home/bob/cyborg && python -m cyborg_server.cli memory cleanup-contacts
```
Expected output reports merges, deletions, rewrites, and enrichment counts.

- [ ] **Step 5: Verify the result**

```bash
ls /home/bob/.config/cyborg/harness/memory/entities/contact/
grep -l "contact_id:" /home/bob/.config/cyborg/harness/memory/entities/contact/*.md | wc -l
```
- The directory should now contain only `contact-{hex8}.md` (canonical) plus legitimate orphans.
- Every canonical file should have a `contact_id:` frontmatter field linking to the full UUID.

- [ ] **Step 6: Run the validation command**

```bash
cd /home/bob/cyborg && python -m cyborg_server.cli memory validate
```
Expected: `valid: true`, no missing fields.

- [ ] **Step 7: Commit**

```bash
git add packages/cyborg-server/cyborg_server/cli.py
git commit -m "Add 'cyborg memory cleanup-contacts' CLI command"
```

---

## Task 11: Update CHANGELOG

**Files:**
- Modify: `CHANGELOG.md` (top of file)

- [ ] **Step 1: Add an entry**

```markdown
## [Unreleased]

### Fixed
- Memory contact entities no longer duplicate: bulletin generator now receives
  the full contacts DB as `known_entities` (heartbeat path was missing this),
  and the prompt forbids inventing `contact-{name-slug}` / `unresolved-` /
  `unknown-` IDs when a known contact matches. A new reconciliation step
  during claim → entity update remaps any non-canonical contact subject_id to
  canonical via display_name lookup.
- New CLI command `cyborg memory cleanup-contacts` removes existing duplicate
  contact entities, merges their content into the canonical record, rewrites
  every reference (claims, bulletin entity refs, related-entities sections),
  and writes `contact_id`/`email`/`phone_number` into canonical entity
  frontmatter as a real foreign key back to the contacts DB.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "Document memory contact dedup fix in CHANGELOG"
```

---

## Self-Review Checklist (executed by plan author)

- [x] **Prevention: heartbeat known_entities** — Task 3
- [x] **Prevention: stronger prompt** — Task 4
- [x] **Prevention: reconciliation in entity update** — Task 5
- [x] **Forward linkage: contact_id/email/phone in frontmatter** — Tasks 5 (write path) + 9 (existing entities)
- [x] **Cleanup: build renaming map** — Task 7
- [x] **Cleanup: merge entity bodies** — Tasks 6 + 9
- [x] **Cleanup: rewrite claims** — Task 8
- [x] **Cleanup: rewrite bulletin entity refs** — Task 8
- [x] **Cleanup: rewrite entity Related Entities** — Task 8
- [x] **Cleanup: enrich canonical entities with FK** — Task 9
- [x] **CLI command + dry-run** — Task 10
- [x] **CHANGELOG** — Task 11
- [x] **No placeholders** — every step has runnable code or exact commands.
- [x] **Type consistency** — `ContactDirectory`, `ContactRecord`, `reconcile_contact_id`, `build_renaming_map`, `merge_entity_docs`, `run_cleanup` signatures match across tasks.

**Out of scope for this plan (and explicit non-goals):**

- Merging non-contact entities (groups, locations, trips). The same pattern applies but no duplicates were reported in those categories — leave for later if needed.
- Backfilling `aliases/aliases.yml` from the renaming map. The aliases mechanism is unused by the pipeline today; adding to it would be busywork. Reconciliation goes through the live DB instead.
- Changing the bulletin immutability model (we rewrite entity refs in existing bulletins, which is a deliberate exception to "bulletins are immutable" — the alternative is keeping bad references forever).
