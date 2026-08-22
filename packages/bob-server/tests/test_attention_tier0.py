"""Golden tests for Attention Tier 0 addressed detection + shadow recording
(Bob3 Phase III). The name-variant grammar is enumerated: these cases ARE
the spec — extend the table when a new pattern is added.
"""

from __future__ import annotations

import pytest

from bob_server.services.attention import detect_addressed, record_shadow_decision

BOT = "Bob"

ADDRESSED_GROUP_CASES = [
    ("Bob, can you check the weather?", "name_variant"),
    ("bob: status update please", "name_variant"),
    ("Bob what time is the flight?", "name_variant"),
    ("hey bob how's it going", "name_variant"),
    ("Hi Bob, quick question", "name_variant"),
    ("ok bob do it", "name_variant"),
    ("thanks bob!", "name_variant"),
    ("@bob remind me at 6", "name_variant"),
    ("can you help with this, bob?", "name_variant"),
    ("what do you reckon bob?", "name_variant"),
    ("BOB?", "name_variant"),
]

NOT_ADDRESSED_GROUP_CASES = [
    "did anyone see the game last night?",
    "sarah what's up?",
    "tell David the plan changed",
    "I met a guy called Bobby yesterday",       # substring must not match
    "the bobsled team won gold",                # substring must not match
    "Bob's report was great",                   # possessive third-person mention
    "we should ask him tomorrow",
    "",
]


@pytest.mark.parametrize("text,reason", ADDRESSED_GROUP_CASES)
def test_group_addressed_variants(text, reason):
    res = detect_addressed(text, bot_name=BOT, chat_kind="group")
    assert res.addressed, f"should be addressed: {text!r}"
    assert res.reason == reason


@pytest.mark.parametrize("text", NOT_ADDRESSED_GROUP_CASES)
def test_group_not_addressed(text):
    res = detect_addressed(text, bot_name=BOT, chat_kind="group")
    assert not res.addressed, f"should NOT be addressed: {text!r}"
    assert res.reason == "not_addressed"


def test_dm_always_addressed():
    res = detect_addressed("anything at all", bot_name=BOT, chat_kind="dm")
    assert res.addressed and res.reason == "dm"


def test_jid_mention_beats_text():
    res = detect_addressed(
        "random chatter", bot_name=BOT, chat_kind="group",
        bot_jid="614999@s.whatsapp.net",
        mentioned_jids=["614999@s.whatsapp.net"])
    assert res.addressed and res.reason == "mention_jid"


def test_reply_to_bot():
    res = detect_addressed(
        "yes exactly", bot_name=BOT, chat_kind="group", reply_to_bot=True)
    assert res.addressed and res.reason == "reply_to_bot"


# ------------------------------------------------------------------- shadow


async def test_shadow_records_act_for_dm(db):
    await record_shadow_decision(
        db, session_key="agent:main:whatsapp:dm:614", source="whatsapp",
        text="hello", chat_kind="dm", bot_name=BOT)
    row = await db.fetch_one("SELECT * FROM attention_shadow")
    assert row["decision"] == "ACT"
    assert row["addressed"] == 1
    assert row["addressed_reason"] == "dm"
    assert row["proposed_window_ms"] == 10_000


async def test_shadow_wait_for_unaddressed_group_and_micro_window_when_addressed(db):
    await record_shadow_decision(
        db, session_key="agent:main:whatsapp:group:g1", source="whatsapp",
        text="anyone up for lunch?", chat_kind="group", bot_name=BOT)
    await record_shadow_decision(
        db, session_key="agent:main:whatsapp:group:g1", source="whatsapp",
        text="bob, book the table", chat_kind="group", bot_name=BOT)
    rows = await db.fetch_all("SELECT * FROM attention_shadow ORDER BY id")
    assert [r["decision"] for r in rows] == ["WAIT", "ACT"]
    assert rows[0]["proposed_window_ms"] == 20_000
    assert rows[1]["proposed_window_ms"] == 2_500


async def test_shadow_never_raises_on_db_failure(db):
    class Broken:
        async def execute(self, *a, **k):
            raise RuntimeError("boom")
    await record_shadow_decision(
        Broken(), session_key="k", source="whatsapp", text="x", chat_kind="dm")
