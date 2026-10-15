"""Reply re-threading via In-Reply-To/References + automated-mail suppression.

Both pinned to the 2026-10-10 Printful (Zendesk) support exchange: AgentMail
opened a fresh thread per support reply, and the closing satisfaction survey
got a Bob reply. Zendesk stamps the same Auto-Submitted header on human and
survey mail, so the real-reply case must keep dispatching.
"""

from __future__ import annotations

import uuid

import pytest

from server.services.email_polling_service import (
    EMAIL_TURN_PROMPT,
    _header_message_ids,
    automated_email_reason,
)
from tests.services.test_email_inbound_characterization import (
    _capture_create_task,
    _make_service,
    _message,
    _seed_contact,
    _seed_inbox,
)

ZENDESK_HEADERS = {
    "Auto-Submitted": "auto-generated",
    "X-Mailer": "Zendesk Mailer",
    "X-Auto-Response-Suppress": "All",
}


async def _seed_origin_thread(db, inbox: dict, *, agentmail_thread_id: str, origin: str) -> None:
    """An outbound-initiated thread plus Bob's opening message, as
    EmailDeliveryService.send_new_email leaves them."""
    await db.execute(
        """INSERT INTO email_threads (id, inbox_id, agentmail_thread_id, subject,
               session_key, agenda, origin_session_key, created_at, updated_at)
           VALUES (?, ?, ?, 'Stuck tasks', ?, 'Chase the stuck tasks', ?,
                   datetime('now'), datetime('now'))""",
        (str(uuid.uuid4()), inbox["id"], agentmail_thread_id,
         f"agent:main:email:thread:{agentmail_thread_id}", origin))
    await db.execute(
        """INSERT INTO email_messages (id, inbox_id, agentmail_message_id, thread_id,
               subject, sender_email, text_body, message_timestamp, created_at)
           VALUES (?, ?, '<bob-open@ses>', ?, 'Stuck tasks', 'bob@example.com',
                   'hello', '2026-10-10T00:29:45+00:00', datetime('now'))""",
        (str(uuid.uuid4()), inbox["id"], agentmail_thread_id))


def test_header_ids_split_space_joined_in_reply_to_and_references():
    msg = {"in_reply_to": "<a@x> <b@y>", "references": ["<c@z>", "<a@x>"]}
    assert _header_message_ids(msg) == ["<a@x>", "<b@y>", "<c@z>"]
    assert _header_message_ids({}) == []


async def test_reply_under_new_agentmail_thread_joins_the_origin_thread(ctx, db, monkeypatch):
    inbox = await _seed_inbox(db)
    await _seed_origin_thread(db, inbox, agentmail_thread_id="orig", origin="agent:main:whatsapp:dm:1")
    svc, _ = _make_service(ctx)
    tasks = _capture_create_task(monkeypatch)

    msg = _message(message_id="<zd-2@zendesk>", thread_id="fresh",
                   sender='"Support" <support@vendor.com>')
    msg["in_reply_to"] = "<bob-open@ses> <zd-1@zendesk>"
    msg["headers"] = ZENDESK_HEADERS
    assert await svc.process_incoming_message(inbox, msg) is True

    assert await db.fetch_one(
        "SELECT 1 FROM email_threads WHERE agentmail_thread_id = 'fresh'") is None
    stored = await db.fetch_one(
        "SELECT thread_id FROM email_messages WHERE agentmail_message_id = '<zd-2@zendesk>'")
    assert stored["thread_id"] == "orig"
    thread = await db.fetch_one("SELECT * FROM email_threads WHERE agentmail_thread_id = 'orig'")
    assert thread["origin_session_key"] == "agent:main:whatsapp:dm:1"
    assert thread["message_count"] == 1
    assert len(tasks) == 1  # human support reply despite Auto-Submitted
    tasks[0].close()


async def test_unmatched_headers_still_open_a_new_thread(ctx, db, monkeypatch):
    inbox = await _seed_inbox(db)
    svc, _ = _make_service(ctx)
    tasks = _capture_create_task(monkeypatch)
    msg = _message(thread_id="fresh")
    msg["in_reply_to"] = "<nobody-we-know@x>"
    assert await svc.process_incoming_message(inbox, msg) is True
    assert await db.fetch_one(
        "SELECT 1 FROM email_threads WHERE agentmail_thread_id = 'fresh'") is not None
    tasks[0].close()


async def test_kill_switch_keeps_agentmail_thread_ids(ctx, db, monkeypatch):
    monkeypatch.setenv("BOB_EMAIL_REPLY_THREADING", "off")
    inbox = await _seed_inbox(db)
    await _seed_origin_thread(db, inbox, agentmail_thread_id="orig", origin="o")
    svc, _ = _make_service(ctx)
    tasks = _capture_create_task(monkeypatch)
    msg = _message(thread_id="fresh")
    msg["in_reply_to"] = "<bob-open@ses>"
    await svc.process_incoming_message(inbox, msg)
    assert await db.fetch_one(
        "SELECT 1 FROM email_threads WHERE agentmail_thread_id = 'fresh'") is not None
    tasks[0].close()


@pytest.mark.parametrize("msg,reason", [
    ({"subject": "Do you have a sec? Rate our support.", "headers": ZENDESK_HEADERS},
     "survey subject"),
    ({"subject": "Re: case", "extracted_text":
      "rate: https://help.vendor.com/hc/en-us/survey_responses/01M4?access_token=x"},
     "survey link"),
    ({"subject": "Away", "headers": {"Auto-Submitted": "auto-replied"}},
     "auto-replied (out-of-office)"),
    ({"subject": "News", "headers": {"Precedence": "bulk"}}, "precedence: bulk"),
    ({"subject": "Receipt", "from": "No Reply <no-reply@shop.com>"},
     "no-reply sender no-reply@shop.com"),
])
def test_automated_mail_is_recognised(msg, reason):
    msg = {"from": "Support <support@vendor.com>", **msg}
    assert automated_email_reason(msg, sender_trusted=False) == reason


def test_zendesk_human_reply_and_trusted_survey_wording_are_not_automated():
    human = {"from": "Support <support@vendor.com>", "subject": "Re: stuck tasks",
             "headers": ZENDESK_HEADERS, "extracted_text": "Hi, Mike, thanks for reaching out"}
    assert automated_email_reason(human, sender_trusted=False) is None
    trusted = {"from": "Mike <mike@example.com>", "subject": "How did we do at the quiz?"}
    assert automated_email_reason(trusted, sender_trusted=True) is None


def test_automated_kill_switch(monkeypatch):
    monkeypatch.setenv("BOB_EMAIL_AUTOMATED_SUPPRESS", "off")
    assert automated_email_reason(
        {"subject": "Rate our support"}, sender_trusted=False) is None


async def test_survey_is_stored_but_gets_no_turn(ctx, db, monkeypatch):
    inbox = await _seed_inbox(db)
    svc, _ = _make_service(ctx)
    tasks = _capture_create_task(monkeypatch)
    dispatched = []

    async def fake_dispatch(*args, **kwargs):
        dispatched.append(args)

    monkeypatch.setattr(svc, "_dispatch_to_llm", fake_dispatch)
    msg = _message(sender='"Support" <support@vendor.com>')
    msg["subject"] = "Do you have a sec? Rate our support."
    assert await svc.process_incoming_message(inbox, msg) is True
    assert len(tasks) == 1
    await tasks[0]
    assert dispatched == []
    assert await db.fetch_one(
        "SELECT 1 FROM email_messages WHERE agentmail_message_id = 'msg-1'") is not None


async def test_trusted_sender_survey_wording_still_dispatches(ctx, db, monkeypatch):
    inbox = await _seed_inbox(db)
    await _seed_contact(db, "mike@example.com", trusted=1, name="Mike")
    svc, _ = _make_service(ctx)
    tasks = _capture_create_task(monkeypatch)
    dispatched = []

    async def fake_dispatch(*args, **kwargs):
        dispatched.append(args)

    monkeypatch.setattr(svc, "_dispatch_to_llm", fake_dispatch)
    msg = _message(sender='"Mike" <mike@example.com>')
    msg["subject"] = "How did we do at the quiz?"
    await svc.process_incoming_message(inbox, msg)
    await tasks[0]
    assert len(dispatched) == 1


def test_email_turn_prompt_carries_identity_and_skip_rules():
    assert "Sign every email as Bob" in EMAIL_TURN_PROMPT
    assert "never sign with their name" in EMAIL_TURN_PROMPT
    assert "email_skip()" in EMAIL_TURN_PROMPT
