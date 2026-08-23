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

WRITE_ALLOWLIST = {
    ("contacts", "routers/contacts.py"),
    ("contacts", "routers/dashboard_api/contacts.py"),
    ("contacts", "routers/phone.py"),
    ("effects", "routers/dashboard_api/ops.py"),
    ("email_inboxes", "routers/email.py"),
    ("email_inboxes", "services/email_polling_service.py"),
    ("email_messages", "services/email_delivery_service.py"),
    ("email_messages", "services/email_polling_service.py"),
    ("email_messages", "services/email_tools.py"),
    ("email_threads", "routers/email.py"),
    ("email_threads", "services/email_delivery_service.py"),
    ("email_threads", "services/email_polling_service.py"),
    ("email_threads", "services/email_tools.py"),
    ("event_log", "heartbeat.py"),
    ("llm_call_log", "heartbeat.py"),
    ("llm_call_log", "services/llm_dispatch.py"),
    ("memory_claims", "services/memory_tools.py"),
    ("memory_entities", "services/memory_tools.py"),
    ("memory_entities_fts", "services/memory_tools.py"),
    ("memory_entity_embeddings", "services/memory_tools.py"),
    ("memory_search_log", "routers/dashboard_api/memory.py"),
    ("messages", "routers/dashboard_api/ops.py"),
    ("messages", "services/wake_service.py"),
    ("persona_records", "routers/dashboard_api/persona.py"),
    ("persona_records", "routers/persona.py"),
    ("phone_calls", "heartbeat.py"),
    ("phone_calls", "routers/phone.py"),
    ("phone_calls", "services/subagent_service.py"),
    ("phone_calls", "services/voice_dispatch_service.py"),
    ("phone_calls", "services/voice_session_service.py"),
    ("subagents", "services/subagent_service.py"),
    ("subagents", "services/voice_dispatch_service.py"),
    ("voice_sessions", "services/subagent_service.py"),
    ("whatsappgroup_members", "services/whatsapp_bridge_service/_group_events.py"),
    ("whatsappgroups", "services/memory/service.py"),
    ("whatsappgroups", "services/whatsapp_bridge_service/_group_events.py"),
}
READ_ALLOWLIST = {
    ("agendas", "routers/dashboard_api/conversations.py"),
    ("attention_shadow", "cli/replay_cmds.py"),
    ("attention_shadow", "heartbeat.py"),
    ("attention_shadow", "routers/dashboard_api/conversations.py"),
    ("bindings", "cli/replay_cmds.py"),
    ("bindings", "heartbeat.py"),
    ("bindings", "routers/dashboard_api/conversations.py"),
    ("bindings", "services/context_assembler.py"),
    ("bindings", "services/dream/store.py"),
    ("bindings", "services/memory/service.py"),
    ("bindings", "services/session_service.py"),
    ("bindings", "services/session_tools.py"),
    ("bindings", "services/wake_service.py"),
    ("bindings", "services/whatsapp_outreach_tools.py"),
    ("calendars", "routers/context.py"),
    ("contacts", "heartbeat.py"),
    ("contacts", "routers/contacts.py"),
    ("contacts", "routers/dashboard_api/contacts.py"),
    ("contacts", "routers/dashboard_api/conversations.py"),
    ("contacts", "routers/dashboard_api/phone.py"),
    ("contacts", "routers/phone.py"),
    ("contacts", "services/context_assembler.py"),
    ("contacts", "services/email_tools.py"),
    ("contacts", "services/group_tools.py"),
    ("contacts", "services/memory/service.py"),
    ("contacts", "services/session_tools.py"),
    ("contacts", "services/whatsapp_outreach_tools.py"),
    ("conversations", "routers/dashboard_api/conversations.py"),
    ("conversations", "services/dream/config.py"),
    ("dream_item_links", "services/memory/tools.py"),
    ("dream_item_links", "services/whatsapp_bridge_service/_slash_commands.py"),
    ("dream_plans", "routers/dashboard_api/dreams.py"),
    ("dream_plans", "services/memory/tools.py"),
    ("dream_plans", "services/whatsapp_bridge_service/_slash_commands.py"),
    ("dream_resolutions", "routers/dashboard_api/dreams.py"),
    ("dream_resolutions", "services/memory/tools.py"),
    ("effects", "cli/replay_cmds.py"),
    ("effects", "routers/dashboard_api/conversations.py"),
    ("effects", "routers/dashboard_api/ops.py"),
    ("email_inboxes", "heartbeat.py"),
    ("email_inboxes", "routers/email.py"),
    ("email_inboxes", "services/email_delivery_service.py"),
    ("email_inboxes", "services/email_polling_service.py"),
    ("email_inboxes", "services/email_tools.py"),
    ("email_messages", "heartbeat.py"),
    ("email_messages", "routers/dashboard_api/conversations.py"),
    ("email_messages", "services/email_delivery_service.py"),
    ("email_messages", "services/email_polling_service.py"),
    ("email_messages", "services/email_tools.py"),
    ("email_threads", "routers/dashboard_api/conversations.py"),
    ("email_threads", "routers/email.py"),
    ("email_threads", "services/email_polling_service.py"),
    ("email_threads", "services/email_tools.py"),
    ("email_threads", "services/session_agenda_service.py"),
    ("eval_runs", "cli/eval_cmds.py"),
    ("event_log", "cli/replay_cmds.py"),
    ("event_log", "heartbeat.py"),
    ("event_log", "routers/dashboard_api/ops.py"),
    ("events", "routers/context.py"),
    ("goal_transitions", "routers/dashboard_api/conversations.py"),
    ("goal_transitions", "routers/dashboard_api/goals.py"),
    ("goals", "routers/dashboard_api/conversations.py"),
    ("goals", "routers/dashboard_api/goals.py"),
    ("goals", "routers/dashboard_api/ops.py"),
    ("goals", "services/context_assembler.py"),
    ("goals", "services/whatsapp_bridge_service/_service.py"),
    ("goals", "services/whatsapp_outreach_tools.py"),
    ("llm_call_log", "routers/dashboard_api/calls.py"),
    ("llm_call_log", "routers/dashboard_api/contacts.py"),
    ("llm_call_log", "routers/dashboard_api/conversations.py"),
    ("llm_call_log", "routers/dashboard_api/home.py"),
    ("llm_call_log", "services/llm_dispatch.py"),
    ("llm_call_log", "services/reflection_service.py"),
    ("memory_claims", "heartbeat.py"),
    ("memory_claims", "routers/dashboard_api/contacts.py"),
    ("memory_claims", "routers/dashboard_api/home.py"),
    ("memory_claims", "routers/dashboard_api/memory.py"),
    ("memory_claims", "services/memory_tools.py"),
    ("memory_entities", "cli/memory_cmds.py"),
    ("memory_entities", "heartbeat.py"),
    ("memory_entities", "routers/dashboard_api/home.py"),
    ("memory_entities", "routers/dashboard_api/memory.py"),
    ("memory_entities", "services/memory_tools.py"),
    ("memory_entities_fts", "services/memory_tools.py"),
    ("memory_entity_embeddings", "services/memory_tools.py"),
    ("memory_extraction_turns", "heartbeat.py"),
    ("memory_questions", "routers/dashboard_api/memory.py"),
    ("memory_search_log", "routers/dashboard_api/memory.py"),
    ("messages", "cli/replay_cmds.py"),
    ("messages", "heartbeat.py"),
    ("messages", "routers/dashboard_api/conversations.py"),
    ("messages", "routers/dashboard_api/home.py"),
    ("messages", "routers/dashboard_api/ops.py"),
    ("messages", "services/dream/store.py"),
    ("messages", "services/whatsapp_bridge_service/_service.py"),
    ("participants", "routers/dashboard_api/contacts.py"),
    ("participants", "routers/dashboard_api/conversations.py"),
    ("persona_records", "routers/dashboard_api/persona.py"),
    ("persona_records", "routers/persona.py"),
    ("persona_records", "services/whatsapp_bridge_service/_slash_commands.py"),
    ("phone_calls", "heartbeat.py"),
    ("phone_calls", "routers/dashboard_api/phone.py"),
    ("phone_calls", "routers/phone.py"),
    ("phone_calls", "services/phone_call_result_service.py"),
    ("phone_calls", "services/phone_tools.py"),
    ("phone_calls", "services/subagent_service.py"),
    ("phone_calls", "services/voice_dispatch_service.py"),
    ("skill_delegations", "routers/dashboard_api/skills.py"),
    ("subagents", "routers/dashboard_api/subagents.py"),
    ("subagents", "services/phone_call_result_service.py"),
    ("subagents", "services/subagent_service.py"),
    ("subagents", "services/voice_dispatch_service.py"),
    ("subagents", "services/voice_session_service.py"),
    ("turn_events", "routers/dashboard_api/ops.py"),
    ("turns", "cli/replay_cmds.py"),
    ("turns", "routers/dashboard_api/conversations.py"),
    ("turns", "routers/dashboard_api/ops.py"),
    ("wakeups", "routers/dashboard_api/goals.py"),
    ("wakeups", "routers/dashboard_api/ops.py"),
    ("whatsappgroup_members", "routers/contacts.py"),
    ("whatsappgroup_members", "routers/dashboard_api/contacts.py"),
    ("whatsappgroup_members", "services/context_assembler.py"),
    ("whatsappgroup_members", "services/group_tools.py"),
    ("whatsappgroup_members", "services/memory/service.py"),
    ("whatsappgroup_members", "services/whatsapp_bridge_service/_group_events.py"),
    ("whatsappgroups", "routers/contacts.py"),
    ("whatsappgroups", "routers/dashboard_api/contacts.py"),
    ("whatsappgroups", "routers/dashboard_api/conversations.py"),
    ("whatsappgroups", "services/context_assembler.py"),
    ("whatsappgroups", "services/group_tools.py"),
    ("whatsappgroups", "services/memory/service.py"),
    ("whatsappgroups", "services/session_tools.py"),
    ("whatsappgroups", "services/whatsapp_bridge_service/_group_events.py"),
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
