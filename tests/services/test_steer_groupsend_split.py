"""Structural split of the cross-conversation messaging tools.

Human-started turns carry steer_conversation only; autonomous wake-path
turns carry send_whatsapp_group_message only (docs/steering-plan.md, Bob
Events §1.5). Before the split both attached on trusted human turns and the
model drifted between them — 2026-08-30, one "advertise this set" request
double-posted AI doom via the direct tool while steering Leeming Boys in the
same fan-out, and the steered turn itself fanned out more direct sends.
"""

from __future__ import annotations

DM_KEY = "agent:main:whatsapp:dm:61400000000"
CHAT_ID = "61400000000@s.whatsapp.net"


async def _seed_contact(ctx) -> None:
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, is_trusted, "
        "created_at, updated_at) VALUES ('c-mike', 'Mike', '+61400000000', "
        "1, datetime('now'), datetime('now'))")
    from server.repositories.conversations import ConversationRepository
    await ConversationRepository(ctx.db).register_endpoint(
        DM_KEY, endpoint_kind="dm", contact_id="c-mike")


async def _spec(ctx, **overrides):
    from typing import Any

    from server.services.whatsapp_bridge_service._service import (
        WhatsAppBridgeService)
    kwargs: dict[str, Any] = dict(
        session_key=DM_KEY, chat_id=CHAT_ID, chat_kind="dm",
        contact_id="c-mike", is_trusted=True, human_initiated=True)
    kwargs.update(overrides)
    return await WhatsAppBridgeService(ctx)._build_inbound_dispatch_spec(**kwargs)


def _names(spec) -> set[str]:
    return {t.name for t in spec.tools}


async def test_human_turn_steers_but_cannot_direct_send(ctx):
    await _seed_contact(ctx)
    names = _names(await _spec(ctx))
    assert "steer_conversation" in names
    assert "send_whatsapp_group_message" not in names


async def test_autonomous_trusted_turn_direct_sends_but_cannot_steer(ctx):
    await _seed_contact(ctx)
    names = _names(await _spec(ctx, human_initiated=False))
    assert "send_whatsapp_group_message" in names
    assert "steer_conversation" not in names


async def test_human_turn_any_trust_level_can_steer(ctx):
    """Untrusted humans steer (owner approval); never direct-send."""
    await _seed_contact(ctx)
    names = _names(await _spec(ctx, is_trusted=False))
    assert "steer_conversation" in names
    assert "send_whatsapp_group_message" not in names


async def test_autonomous_untrusted_turn_gets_neither(ctx):
    await _seed_contact(ctx)
    names = _names(await _spec(ctx, is_trusted=False, human_initiated=False))
    assert "steer_conversation" not in names
    assert "send_whatsapp_group_message" not in names


async def test_wake_session_derives_human_initiated_from_pending_rows(
        ctx, monkeypatch):
    """Wake turns count as human-initiated exactly when an undispatched row
    is a raw inbound message (no provenance label) — covers post-call
    occupancy drain and crash recovery of human messages; pure wake content
    (wake_nudge / steer / task_relay rows) stays autonomous, so no nested
    steering even though the route resolves a contact id."""
    await _seed_contact(ctx)

    from server.services.attention import AttentionCoordinator
    from server.services.session_service import SessionService
    from server.services.whatsapp_bridge_service._service import (
        WhatsAppBridgeService)

    recorded: dict[str, object] = {}
    real = WhatsAppBridgeService._build_inbound_dispatch_spec

    async def _spy(self, **kwargs):
        recorded.update(kwargs)
        return await real(self, **kwargs)

    async def _no_dispatch(self, session_key, run):
        recorded.setdefault("dispatched_session", session_key)

    monkeypatch.setattr(
        WhatsAppBridgeService, "_build_inbound_dispatch_spec", _spy)
    monkeypatch.setattr(AttentionCoordinator, "resume_pending", _no_dispatch)

    svc = WhatsAppBridgeService(ctx)
    sessions = SessionService(ctx)

    # Pure wake content: autonomous.
    await sessions.add_message(
        DM_KEY, "user", "[Steering request — Mike, via Pirate Radio]",
        channel="whatsapp", dispatched=0, provenance="steer")
    await svc.wake_session(DM_KEY)
    assert recorded["human_initiated"] is False

    # A queued human inbound row joins the turn: human-initiated.
    await sessions.add_message(
        DM_KEY, "user", "also tell them about the poster",
        channel="whatsapp", sender_id="c-mike", dispatched=0)
    await svc.wake_session(DM_KEY)
    assert recorded["human_initiated"] is True
