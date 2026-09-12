"""Detach v2 attribution rendering (docs/detach-v2.md).

The labels are a correctness dependency, not polish: if a [bg] row or the
detach placeholder renders unlabelled in any prompt path, later turns
silently misattribute background speech to the live voice — the exact
confusion the 2026-09-11 coffee-gif incident was made of.
"""

from __future__ import annotations

import json

from server.services.prompt_assembler import (
    BG_PLACEHOLDER_MARKER, build_chat_messages)
from server.services.session_service import SessionService

DM_KEY = "agent:main:whatsapp:dm:61400000000"


async def _messages_content(ctx) -> str:
    built = await build_chat_messages(
        "what's the latest?", DM_KEY, db=ctx.db, max_history=20)
    # ensure_ascii=False: the markers carry em-dashes that would otherwise
    # hide behind — escapes and match nothing.
    return json.dumps([m.get("content", "") for m in built],
                      ensure_ascii=False)


async def test_bg_placeholder_and_sends_render_labelled(ctx):
    svc = SessionService(ctx)
    await svc.add_message(
        DM_KEY, "user", "share the coffee gif over active groups",
        channel="whatsapp", dispatched=1)
    await svc.add_message(
        DM_KEY, "user",
        "[bg task 4545bcaa detached: sharing a coffee gif. Its messages "
        "will appear under that id until it finishes.]",
        channel="whatsapp", provenance="bg_placeholder", dispatched=1)
    await svc.add_message(
        DM_KEY, "assistant", "☕ clip going out",
        channel="whatsapp", provenance="bg_send",
        metadata={"bg_task": "4545bcaa-1234-5678-90ab"}, dispatched=1)
    await svc.add_message(
        DM_KEY, "assistant", "anything else while that runs?",
        channel="whatsapp", dispatched=1)

    rendered = await _messages_content(ctx)
    assert BG_PLACEHOLDER_MARKER.strip()[:30] in rendered, (
        "the placeholder must announce the flight in every replay")
    assert "[bg 4545bcaa] ☕ clip going out" in rendered, (
        "the flight's sends must carry their [bg id] tag")
    # The live voice's own rows stay untagged
    assert "[bg anything else" not in rendered


async def test_bg_rows_render_on_non_dispatch_replays_too(ctx):
    """Attribution must never silently vanish: subagent/wake replays
    (claimed_ids=None) label bg rows just the same."""
    svc = SessionService(ctx)
    await svc.add_message(
        DM_KEY, "assistant", "background result report",
        channel="whatsapp", provenance="bg_send",
        metadata={"bg_task": "aaaabbbb-1"}, dispatched=1)
    built = await build_chat_messages("status?", DM_KEY, db=ctx.db)
    rendered = json.dumps([m.get("content", "") for m in built])
    assert "[bg aaaabbbb] background result report" in rendered


async def test_group_send_from_flight_tags_target_history(ctx, tmp_path,
                                                          monkeypatch):
    """Part 5: a detached flight's proactive group send lands in the
    TARGET group's history with the [bg id] tag (content-level — renders
    everywhere with no renderer dependency)."""
    from tests.services.test_group_send_media import (
        GROUP_ID, GROUP_KEY, _capture_emitter, _seed_group, _tools)

    await _seed_group(ctx)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)
    _capture_emitter(monkeypatch)

    from server.services.whatsapp_outreach_tools import make_group_send_tools

    class _Wa:
        connected = True

    flight = {"subagent_id": "99887766-abcd", "sent": False}
    tools = {t.name: t.handler for t in make_group_send_tools(
        ctx, _Wa(), "agent:main:whatsapp:dm:61400000000", flight=flight)}
    import json as _json
    out = _json.loads(await tools["send_whatsapp_group_message"](
        group_id=GROUP_ID, message="morning update"))
    assert out["ok"]

    from server.services.session_service import SessionService
    msgs = await SessionService(ctx).get_messages(GROUP_KEY, limit=5)
    assert any(m.content.startswith("[bg 99887766] morning update")
               for m in msgs), (
        "the target transcript must show which background task spoke")
