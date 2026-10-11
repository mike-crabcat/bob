"""The session agenda reaches the prompt on every channel that offers
update_agenda (2026-10-08 eval finding: only email rendered it — on
WhatsApp and in rooms Bob wrote an agenda he could never read back)."""

from __future__ import annotations

import asyncio

from server.services.context_assembler import ContextAssembler
from server.services.session_agenda_service import SessionAgendaService

DM_KEY = "agent:main:whatsapp:dm:61400000000"
AGENDA = "## Task List\n1. Calculate 1+1 and report the result"


async def test_agenda_block_empty_without_agenda(ctx):
    assert await ContextAssembler(ctx).agenda_block(DM_KEY) == ""


async def test_whatsapp_spec_carries_the_agenda(ctx):
    from server.repositories.conversations import ConversationRepository
    from server.services.whatsapp_bridge_service._service import WhatsAppBridgeService
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, "
        "updated_at) VALUES ('c-mike', 'Mike', '+61400000000', 1, "
        "datetime('now'), datetime('now'))")
    await ConversationRepository(ctx.db).register_endpoint(
        DM_KEY, endpoint_kind="dm", contact_id="c-mike")
    await SessionAgendaService(ctx).set_agenda(DM_KEY, AGENDA)
    spec = await WhatsAppBridgeService(ctx)._build_inbound_dispatch_spec(
        session_key=DM_KEY, chat_id="61400000000@s.whatsapp.net", chat_kind="dm",
        contact_id="c-mike", is_trusted=True, human_initiated=True)
    assert "Session agenda" in spec.system_content
    assert "Calculate 1+1" in spec.system_content
    assert "update_agenda" in {t.name for t in spec.tools}


async def test_generic_wake_carries_the_agenda(ctx, monkeypatch):
    from server.services import wake_service
    from server.services.llm_dispatch import LLMDispatchService
    ctx.settings.openai.api_key = ctx.settings.openai.api_key or "sk-test"
    seen = []

    async def fake_run_turn(self, messages, tools, **kw):
        seen.append(messages)
        return "ok"
    monkeypatch.setattr(LLMDispatchService, "run_turn", fake_run_turn)
    await SessionAgendaService(ctx).set_agenda("conv-internal-agenda", AGENDA)
    await wake_service.wake_conversation(ctx, "conv-internal-agenda", "tick")
    await asyncio.gather(*list(wake_service._pending_dispatches))
    assert "Calculate 1+1" in seen[0][0]["content"]
