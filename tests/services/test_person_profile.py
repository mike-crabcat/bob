"""DM person profile (2026-10-06): rendered claims, resolved by contact_id
claim → name; ambiguous names get nothing (never another person's facts)."""

from __future__ import annotations

from server.services.context_assembler import ContextAssembler


async def _contact(db, cid, name, phone):
    await db.execute("INSERT INTO contacts (id, name, phone_number, created_at, updated_at) "
                     "VALUES (?, ?, ?, datetime('now'), datetime('now'))", (cid, name, phone))


async def _person(db, eid, name, claims):
    await db.execute("INSERT INTO memory_entities (entity_id, entity_type, display_name, status) "
                     "VALUES (?, 'person', ?, 'active')", (eid, name))
    for i, (k, v) in enumerate(claims):
        await db.execute(
            "INSERT INTO memory_claims (id, claim_type_key, subject_id, value, status, created_at) "
            "VALUES (?, ?, ?, ?, 'active', datetime('now'))", (f"c-{eid}-{i}", k, eid, v))


async def test_profile_renders_facts_not_an_id(ctx, db):
    await _contact(db, "c-syl", "Sylvain", "+61400000101")
    await _person(db, "person-sylvain", "Sylvain",
                  [("work_schedule", "Works from home Thursdays only")])
    out = await ContextAssembler(ctx).person_profile("c-syl")
    assert "Works from home Thursdays only" in out
    assert out.strip() != "## Person Profile\n\nperson-sylvain"


async def test_ambiguous_name_gets_no_profile(ctx, db):
    await _contact(db, "c-chris-au", "Chris", "+61400000102")
    await _contact(db, "c-chris-ca", "Chris", "+17780000000")
    await _person(db, "person-chris", "Chris", [("workplace", "Somewhere private")])
    assert await ContextAssembler(ctx).person_profile("c-chris-ca") == ""


async def test_unknown_contact_is_empty(ctx, db):
    assert await ContextAssembler(ctx).person_profile("nope") == ""
