"""Dispatch-claim marking in prompt assembly (2026-08-30 GLM duplication fix).

A follow-up turn (mid-turn arrival claimed by the attention coordinator's
leftover sweep, or a system wake) used to send the LLM an input ending with
the PRIOR turn's assistant reply and the new stimulus buried unmarked
mid-replay — GLM-5.3-flash regenerated its own last reply verbatim on ~11%
of turns, gpt-5.6-sol on ~3.6%. Pinned here, at the build_chat_messages seam:

- mid-turn-arrival shape ends user-final, with the claimed rows marked [NEW]
  and re-presented in a trailing user turn carrying the don't-repeat contract
- normal idle turns are untouched (their pending rows are already trailing)
- claimed wake nudges never become the trailing stimulus: nudge-only turns
  get a system directive to fold silently, mixed turns lift only the human
  rows
- system-generated rows (wake_nudge, group_event) are labelled in replay
  whether or not they are the stimulus, so they never read as human speech
- group events stay first-class stimuli (greeting case): claimed, lifted,
  and hinted
"""

from __future__ import annotations

import pytest

from server.repositories.history import HistoryRepository
from server.services.prompt_assembler import build_chat_messages
from server.services.session_service import SessionService


async def _pending_ids(db, key: str) -> set[str]:
    return set(await HistoryRepository(db).pending_user_ids(key))


async def _build(ctx, db, key: str, **kwargs):
    return await build_chat_messages(
        None, key, db=db, system_content="sys", max_history=50, **kwargs)


def _contents(messages, role: str) -> list[str]:
    return [m["content"] for m in messages
            if m.get("role") == role and isinstance(m.get("content"), str)]


@pytest.mark.asyncio
async def test_mid_turn_arrival_ends_user_final_with_new_marks(ctx, db):
    """The duplicate shape: new message arrived while a turn was in flight,
    so replay ends with that turn's reply. The input must end user-final and
    carry the claimed message + contract."""
    key = "test:claim:mid-turn"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "first question", dispatched=0)
    await svc.add_message(key, "user", "what about the second thing?", dispatched=0)
    await svc.add_message(key, "assistant", "Answer to the first question.")

    claimed = await _pending_ids(db, key)
    assert len(claimed) == 2
    messages = await _build(ctx, db, key, claimed_ids=claimed,
                            send_tool_name="send_whatsapp_message")

    assert messages[-1]["role"] == "user", "input must end user-final"
    trailer = messages[-1]["content"]
    assert "what about the second thing?" in trailer
    assert "already delivered" in trailer
    assert "do not repeat" in trailer
    assert "NO_REPLY" in trailer
    assert "send_whatsapp_message" in trailer
    # The claimed rows are also marked in their chronological slot.
    assert any("[NEW — awaiting your reply]" in c for c in _contents(messages, "user"))


@pytest.mark.asyncio
async def test_idle_turn_unchanged(ctx, db):
    """Normal case: the pending message IS the trailing row — no synthetic
    trailer, just the [NEW] marker."""
    key = "test:claim:idle"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "hello!", dispatched=0)

    claimed = await _pending_ids(db, key)
    messages = await _build(ctx, db, key, claimed_ids=claimed,
                            send_tool_name="send_whatsapp_message")

    user_msgs = _contents(messages, "user")
    assert len(user_msgs) == 1, "no synthetic trailer on the idle shape"
    assert user_msgs[0].startswith("[NEW — awaiting your reply]")


@pytest.mark.asyncio
async def test_nudge_only_turn_gets_silent_directive(ctx, db):
    """Goal folds and other system wakes: never a trailing user stimulus —
    a system directive instead, and the nudge labelled in replay."""
    key = "test:claim:nudge-only"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "## Goal progress\nfold this",
                          dispatched=0, provenance="wake_nudge")
    await svc.add_message(key, "assistant", "Folded once already.")

    claimed = await _pending_ids(db, key)
    messages = await _build(ctx, db, key, claimed_ids=claimed,
                            send_tool_name="send_whatsapp_message")

    assert messages[-1]["role"] == "system", "nudge-only turns end with the directive"
    assert "not by a message from the human" in messages[-1]["content"]
    assert "NO_REPLY" in messages[-1]["content"]
    # The nudge row replays labelled, not as plain human speech.
    assert any(c.startswith("[system notification — not from the human]")
               for c in _contents(messages, "user"))
    # No [NEW] stimulus marker on a nudge.
    assert not any("[NEW" in c for c in _contents(messages, "user"))


@pytest.mark.asyncio
async def test_mixed_nudge_and_human_lifts_only_human(ctx, db):
    """A nudge racing a real inbound: the human row is the trailing
    stimulus; the nudge stays in-slot as a labelled notification."""
    key = "test:claim:mixed"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "## Goal progress\nnudge text",
                          dispatched=0, provenance="wake_nudge")
    await svc.add_message(key, "user", "queue vampire weekend", dispatched=0)
    await svc.add_message(key, "assistant", "Prior reply already sent.")

    claimed = await _pending_ids(db, key)
    messages = await _build(ctx, db, key, claimed_ids=claimed,
                            send_tool_name="send_whatsapp_message")

    assert messages[-1]["role"] == "user"
    trailer = messages[-1]["content"]
    assert "queue vampire weekend" in trailer
    assert "nudge text" not in trailer, "nudges never ride the trailing stimulus"
    # ...but the nudge is still visible in-slot, labelled.
    assert any("## Goal progress" in c and c.startswith("[system notification")
               for c in _contents(messages, "user"))


@pytest.mark.asyncio
async def test_group_event_claimed_is_lifted_and_hinted(ctx, db):
    """Member changes are greeting-worthy stimuli: claimed, lifted into the
    trailing turn, and carrying the it's-ok-to-stay-silent hint."""
    key = "test:claim:group-event"
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user", "## Group Member Change\nMembers joined: Dana",
        dispatched=0, provenance="group_event")
    await svc.add_message(key, "assistant", "Welcome aboard earlier folks.")

    claimed = await _pending_ids(db, key)
    messages = await _build(ctx, db, key, claimed_ids=claimed,
                            send_tool_name="send_whatsapp_message")

    assert messages[-1]["role"] == "user"
    trailer = messages[-1]["content"]
    assert "Members joined: Dana" in trailer
    assert "greet a new member" in trailer
    # In-slot: NEW marker + group-event label, both present.
    assert any(c.startswith("[NEW — awaiting your reply] [Group event]")
               for c in _contents(messages, "user"))


@pytest.mark.asyncio
async def test_historical_system_rows_labelled(ctx, db):
    """Older nudges/group events (not this turn's claims) still replay with
    their labels so they never read as human speech in later turns."""
    key = "test:claim:historical"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "## Goal completed\nold news",
                          provenance="wake_nudge")  # dispatched=1 default: answered
    await svc.add_message(key, "assistant", "Noted, closed it out.")
    await svc.add_message(key, "user", "## Group Member Change\nMembers left: Eve",
                          provenance="group_event", dispatched=1)
    await svc.add_message(key, "user", "fresh human message", dispatched=0)

    claimed = await _pending_ids(db, key)
    assert len(claimed) == 1
    messages = await _build(ctx, db, key, claimed_ids=claimed,
                            send_tool_name="send_whatsapp_message")

    user_msgs = _contents(messages, "user")
    assert any(c.startswith("[system notification — not from the human]")
               for c in user_msgs)
    assert any(c.startswith("[Group event]") for c in user_msgs)
    # Only the fresh human message is marked NEW.
    new_marked = [c for c in user_msgs if "[NEW" in c]
    assert new_marked == ["[NEW — awaiting your reply] fresh human message"]


@pytest.mark.asyncio
async def test_no_claimed_ids_leaves_replay_untouched(ctx, db):
    """Non-dispatch callers (no claimed_ids): byte-for-byte the old shape —
    no markers, no trailers."""
    key = "test:claim:passthrough"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "## Goal progress\nold nudge",
                          provenance="wake_nudge")
    await svc.add_message(key, "assistant", "done")
    await svc.add_message(key, "user", "hello", dispatched=0)

    messages = await _build(ctx, db, key)
    assert _contents(messages, "user") == ["## Goal progress\nold nudge", "hello"]


@pytest.mark.asyncio
async def test_dispatch_runner_marks_claims_end_to_end(ctx, db, monkeypatch):
    """The runner passes its claimed ids through: a real dispatch turn over
    the mid-turn-arrival shape sends the LLM a user-final input whose trailer
    re-presents the new stimulus."""
    from server.services.dispatch_runner import DispatchRunner, DispatchSpec
    from server.services.llm_dispatch import LLMDispatchService

    key = "test:claim:runner"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "mid-turn human message", dispatched=0)
    await svc.add_message(key, "assistant", "prior turn reply")

    seen: dict = {}

    async def _chat(self, messages, tools, **kwargs):
        seen["messages"] = messages
        return ""

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", _chat)

    spec = DispatchSpec(
        session_key=key, system_content="sys", tools=[],
        call_category="whatsapp_incoming",
        send_tool_name="send_whatsapp_message",
        dispatch_id="test-dispatch",
        message_was_sent=[False], sent_texts=[],
    )
    await DispatchRunner(ctx).run(spec)

    assert seen["messages"], "LLM must have been called"
    assert seen["messages"][-1]["role"] == "user", \
        "the shipped input must end user-final"
    assert "mid-turn human message" in seen["messages"][-1]["content"]
