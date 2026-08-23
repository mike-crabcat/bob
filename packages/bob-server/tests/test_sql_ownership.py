"""SQL ownership enforcement (db-access-cleanup-plan.md increment 1).

Every table has an explicit owner set: the only modules allowed to run SQL
against it. Writes are enforced strictly; reads are allowed from the owner
set plus any repository (repositories may JOIN other tables read-only).

The allowlists below are the known legacy violations. They only shrink —
each cleanup increment ports call sites and deletes their entries. Adding
a NEW entry to an allowlist requires a documented reason.

Detector notes: scans string constants (and f-string constant parts) in
bob_server/**/*.py via ast, skipping docstrings and comments. SQL keywords
are matched case-sensitively (uppercase), so prose does not false-positive.
Dynamic table names (f-string placeholders) are invisible to the scanner
by construction — annotate such sites with a nearby comment and keep the
table list they iterate static.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import bob_server

PKG_ROOT = Path(bob_server.__file__).parent

# Target owner sets. Prefixes (trailing /) own whole packages. Paths that do
# not exist yet (e.g. repositories/phone_calls.py) are planned owners from
# db-access-cleanup-plan.md; until they land, call sites live in allowlists.
OWNERS: dict[str, list[str]] = {
    "messages": ["services/session_service.py", "repositories/history.py"],
    "bindings": ["repositories/conversations.py"],
    "conversations": ["repositories/conversations.py"],
    "participants": ["repositories/participants.py"],
    "agendas": ["repositories/participants.py"],
    "contacts": ["repositories/contacts.py"],
    "turns": ["repositories/turns.py"],
    "turn_events": ["repositories/turns.py"],
    "effects": ["repositories/effects.py", "services/effects.py"],
    "event_log": ["repositories/event_log.py"],
    "goals": ["repositories/goals.py"],
    "goal_transitions": ["repositories/goals.py"],
    "wakeups": ["repositories/wakeups.py"],
    "attention_shadow": ["services/attention/"],
    "llm_call_log": ["repositories/llm_call_log.py"],
    "phone_calls": ["repositories/phone_calls.py"],
    "whatsappgroups": ["repositories/groups.py"],
    "whatsappgroup_members": ["repositories/groups.py"],
    "subagents": ["repositories/subagents.py"],
    "voice_sessions": ["services/voice_session_service.py"],
    "email_threads": ["services/email_store.py"],
    "email_messages": ["services/email_store.py"],
    "email_inboxes": ["services/email_store.py"],
    "calendars": ["services/calendar_service.py"],
    "events": ["services/calendar_service.py"],
    "event_recipients": ["services/calendar_service.py"],
    "webhook_configs": ["services/webhook_service.py"],
    "webhook_deliveries": ["services/webhook_service.py"],
    "routines": ["services/routine_service.py"],
    "persona_records": ["services/persona.py"],
    "skill_delegations": ["services/skill_developer_service.py"],
    "location_history": ["heartbeat.py", "services/location_tools.py"],
    "recon_model_overrides": ["services/memory/", "cli/memory_cmds.py"],
}

# Table-name-prefix families owned by whole packages.
PREFIX_OWNERS: dict[str, list[str]] = {
    "dream_": ["services/dream/"],
    "memory_": ["services/memory/"],
    "eval_": ["evals/"],
}

IGNORED_TABLES = {"sqlite_sequence", "schema_migrations"}

# All writes go through the owning repository/domain store. Keep this empty.
WRITE_ALLOWLIST: set[tuple[str, str]] = set()

# Documented cross-domain READ seams. Each group below is a deliberate
# exception; anything new must be justified here or ported to a repository.
READ_ALLOWLIST = {
    # Operator debug CLI: read-only forensic queries across the event
    # pipeline. Not part of the serving path.
    ("attention_shadow", "cli/replay_cmds.py"),
    ("effects", "cli/replay_cmds.py"),
    ("event_log", "cli/replay_cmds.py"),
    ("turns", "cli/replay_cmds.py"),
    ("eval_runs", "cli/eval_cmds.py"),
    # heartbeat: daily attention-shadow agreement telemetry (Phase III soak).
    ("attention_shadow", "heartbeat.py"),
    # email_store is a domain store (not under repositories/) — its thread
    # search joins contacts for display names, a sanctioned read.
    ("contacts", "services/email_store.py"),
    # dream config resolves a session's conversation row for autoplan routing.
    ("conversations", "services/dream/config.py"),
}


_WRITE_RE = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+[A-Z]+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+([a-zA-Z_]\w*)")
_READ_RE = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_]\w*)")


def _sql_strings(path: Path):
    """Yield non-docstring string constants (incl. f-string constant parts)."""
    tree = ast.parse(path.read_text())
    doc_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc_ids.add(id(body[0].value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in doc_ids):
            yield node.value


def _owners_for(table: str) -> list[str] | None:
    if table in OWNERS:
        return OWNERS[table]
    for prefix, owners in PREFIX_OWNERS.items():
        if table.startswith(prefix):
            return owners
    return None


def _owned(table: str, rel: str) -> bool:
    owners = _owners_for(table)
    assert owners is not None
    return any(rel.startswith(o) if o.endswith("/") else rel == o for o in owners)


async def test_sql_ownership(db):
    rows = await db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r["name"] for r in rows} - IGNORED_TABLES
    tables = {t for t in tables if not t.endswith("_fts") or t in tables}

    unowned = {t for t in tables if _owners_for(t) is None
               # FTS5/vec0 shadow tables inherit their base table's family
               and not any(t.startswith(p) for p in PREFIX_OWNERS)}
    assert not unowned, f"tables with no owner entry (add to OWNERS): {sorted(unowned)}"

    write_viol: list[tuple[str, str]] = []
    read_viol: list[tuple[str, str]] = []
    for path in sorted(PKG_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(PKG_ROOT))
        for s in _sql_strings(path):
            for m in _WRITE_RE.finditer(s):
                t = m.group(1)
                if t in tables and not _owned(t, rel) and (t, rel) not in WRITE_ALLOWLIST:
                    write_viol.append((t, rel))
            for m in _READ_RE.finditer(s):
                t = m.group(1)
                if (t in tables and not _owned(t, rel)
                        and not rel.startswith("repositories/")
                        and (t, rel) not in READ_ALLOWLIST):
                    read_viol.append((t, rel))

    msg = []
    if write_viol:
        msg.append("Unsanctioned WRITES (move into the owning module, or justify "
                   "an allowlist entry): " + str(sorted(set(write_viol))))
    if read_viol:
        msg.append("Unsanctioned READS: " + str(sorted(set(read_viol))))
    assert not msg, "\n".join(msg)


async def test_allowlists_have_no_stale_entries(db):
    """Entries whose violation no longer exists must be deleted (the ratchet)."""
    rows = await db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r["name"] for r in rows} - IGNORED_TABLES
    live: set[tuple[str, str]] = set()
    for path in sorted(PKG_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(PKG_ROOT))
        for s in _sql_strings(path):
            for m in _WRITE_RE.finditer(s):
                if m.group(1) in tables:
                    live.add((m.group(1), rel))
            for m in _READ_RE.finditer(s):
                if m.group(1) in tables:
                    live.add((m.group(1), rel))
    stale = (WRITE_ALLOWLIST | READ_ALLOWLIST) - live
    assert not stale, f"stale allowlist entries (delete them): {sorted(stale)}"
