"""send_whatsapp_group_message media support + text-optional media sends
(2026-09-11 coffee-gif incident: the direct fan-out path TypeError'd on
media_path while five steered turns delivered five different random gifs —
the payload route must be able to carry payloads)."""

from __future__ import annotations

import json

DM_KEY = "agent:main:whatsapp:dm:61400000000"
GROUP_KEY = "agent:main:whatsapp:group:120000000000000"
GROUP_ID = "120000000000000"


class _WaStub:
    connected = True


async def _tools(ctx):
    from server.services.whatsapp_outreach_tools import make_group_send_tools
    return {t.name: t.handler for t in make_group_send_tools(
        ctx, _WaStub(), DM_KEY)}


async def _seed_group(ctx):
    from server.repositories.conversations import ConversationRepository
    repo = ConversationRepository(ctx.db)
    await repo.register_endpoint(
        GROUP_KEY, endpoint_kind="group", address=f"{GROUP_ID}@g.us")
    await repo.set_policy(GROUP_KEY, {"group_outbound_enabled": True})


def _capture_emitter(monkeypatch):
    sent: list[tuple[str, dict]] = []

    async def _fake(ctx, *, kind, idempotency_key, payload, turn_id=None):
        sent.append((kind, payload))
        return {"ok": True, "external_result_id": f"r{len(sent)}"}

    import server.services.effects as effects
    monkeypatch.setattr(effects, "emit_and_deliver", _fake)
    return sent


async def test_group_send_media_emits_media_effect(ctx, tmp_path, monkeypatch):
    await _seed_group(ctx)
    clip = tmp_path / "coffee.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)
    sent = _capture_emitter(monkeypatch)

    tools = await _tools(ctx)
    out = json.loads(await tools["send_whatsapp_group_message"](
        group_id=GROUP_ID, message="☕", media_path="coffee.mp4"))
    assert out["ok"] and out["chat_id"] == f"{GROUP_ID}@g.us"
    kind, payload = sent[-1]
    assert kind == "whatsapp_send_media"
    assert payload["chat_id"] == f"{GROUP_ID}@g.us"
    assert payload["caption"] == "☕"
    assert payload["file_path"].endswith("coffee.mp4")
    # the group's history mirror records the media send
    from server.services.session_service import SessionService
    msgs = await SessionService(ctx).get_messages(GROUP_KEY, limit=5)
    assert any("[Image: ☕]" in m.content for m in msgs)


async def test_group_send_text_only_is_unchanged(ctx, monkeypatch):
    await _seed_group(ctx)
    sent = _capture_emitter(monkeypatch)
    tools = await _tools(ctx)
    out = json.loads(await tools["send_whatsapp_group_message"](
        group_id=GROUP_ID, message="plain text"))
    assert out["ok"]
    kind, payload = sent[-1]
    assert kind == "whatsapp_send" and payload["text"] == "plain text"


async def test_group_send_missing_media_is_clean_error(ctx, tmp_path, monkeypatch):
    await _seed_group(ctx)
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)
    sent = _capture_emitter(monkeypatch)
    tools = await _tools(ctx)
    out = json.loads(await tools["send_whatsapp_group_message"](
        group_id=GROUP_ID, message="☕", media_path="nope.mp4"))
    assert not out["ok"] and "nope.mp4" in out["error"]
    assert not sent  # nothing emitted on a resolution failure


async def test_group_send_media_escapes_workspace_rejected(
        ctx, tmp_path, monkeypatch):
    await _seed_group(ctx)
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)
    _capture_emitter(monkeypatch)
    tools = await _tools(ctx)
    out = json.loads(await tools["send_whatsapp_group_message"](
        group_id=GROUP_ID, message="x", media_path="../../etc/passwd"))
    assert not out["ok"] and "escapes workspace" in out["error"]


async def test_insession_send_accepts_media_only(ctx, tmp_path, monkeypatch):
    """The coffee incident's first TypeError: send_whatsapp_message called
    with only media_path. Text is now the optional caption."""
    await _seed_contact(ctx)
    clip = tmp_path / "coffee.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)
    sent = _capture_emitter(monkeypatch)

    from server.services.whatsapp_bridge_service._service import (
        WhatsAppBridgeService)
    spec = await WhatsAppBridgeService(ctx)._build_inbound_dispatch_spec(
        session_key=DM_KEY, chat_id="61400000000@s.whatsapp.net",
        chat_kind="dm", contact_id="c-mike", is_trusted=True,
        human_initiated=True)
    send = next(t for t in spec.tools if t.name == "send_whatsapp_message")
    out = await send.handler(media_path="coffee.mp4")  # no text at all
    assert "Media sent" in out
    kind, payload = sent[-1]
    assert kind == "whatsapp_send_media"
    assert payload["caption"] == ""


async def _seed_contact(ctx) -> None:
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, is_trusted, "
        "created_at, updated_at) VALUES ('c-mike', 'Mike', '+61400000000', "
        "1, datetime('now'), datetime('now'))")
    from server.repositories.conversations import ConversationRepository
    await ConversationRepository(ctx.db).register_endpoint(
        DM_KEY, endpoint_kind="dm", contact_id="c-mike")
