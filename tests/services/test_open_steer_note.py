"""Open steering request restated when its answer arrives (2026-10-11)."""

from server.services.prompt_assembler import build_chat_messages
from server.services.session_service import SessionService

DM = "agent:main:whatsapp:dm:61400000014"


async def test_open_steer_restated_for_the_answer(ctx, db):
    svc = SessionService(ctx)
    await svc.add_message(DM, "user", "[Steering request — Mike] Ask Sylvain his WFH "
                          "day; when he answers, tell the Doom Club group.",
                          provenance="steer", dispatched=1)
    await svc.add_message(DM, "assistant", "Sylvain — which day do you WFH?")
    mid = await svc.add_message(DM, "user", "Thursdays, but office all week in school hols",
                                dispatched=0)
    msgs = await build_chat_messages(None, DM, db=db, claimed_ids={mid},
                                     send_tool_name="send_whatsapp_message")
    notes = [m["content"] for m in msgs if m.get("role") == "system"
             and "Open request in this chat" in str(m.get("content"))]
    assert notes and "Doom Club" in notes[0]


async def test_no_note_without_steer(ctx, db):
    svc = SessionService(ctx)
    mid = await svc.add_message(DM, "user", "hi bob", dispatched=0)
    msgs = await build_chat_messages(None, DM, db=db, claimed_ids={mid})
    assert not any("Open request in this chat" in str(m.get("content")) for m in msgs)
