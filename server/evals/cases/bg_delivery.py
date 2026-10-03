"""Background-delivery eval cases (rewritten for final-text delivery,
2026-10-01 — docs/final-text-delivery-plan.md).

The contract inverted: a turn's FINAL text is the reply, delivered
automatically; send_whatsapp_message is for progress updates and media;
NO_REPLY is the silence primitive. Two pins:

- B1 silence: an explicit "don't reply to this" must finish with the
  NO_REPLY marker — the new silence primitive in place of "just don't
  call the tool".
- B2 flight answers: the detached flight (delivery-note injected between
  rounds, as production detaches) investigates via bash and answers in
  its FINAL text — no send call required. This is the 2026-09-29
  incident's shape with the fix: the answer lands whatever the model
  does, so the pin is on answer quality, not send compliance.

Fixture fidelity: full production workspace prompt + clock (the new
CRITICAL block rides as production renders it), the production send
tool's exact name/description/schema (mock handler), the production
detach note imported from backburner.delivery_note, the shadow tool
crowd, and a planted log tree the flight can genuinely investigate.
"""

from __future__ import annotations

from server.evals.case import JudgeCriteria, StructuralCheck
from server.evals.registry import eval_case
from server.evals.util import extract_tool_calls, make_planted_bash, make_shadow_surface, pinned_model

_DM_FRAMING = "You are Bob in a WhatsApp DM with Mike (trusted)."
_GROUP_FRAMING = ("You are Bob in a WhatsApp group chat with trusted "
                  "friends (Weeming Boys). Messages are prefixed [Name].")

_QUESTION = (
    "Ok so when you made turntables earlier today in this chat did you "
    "use runware to video gen them?")

# What the flight greps: render logs proving the Blender pipeline.
_RENDER_LOGS = {
    "scratch/turntable-render.log": "\n".join([
        "2026-09-29 10:12:03 turntable figurine=mike-shark "
        "engine=Blender-4.0.2 renderer=Cycles samples=64 frames=8",
        "2026-09-29 10:41:57 turntable figurine=david-gnome "
        "engine=Blender-4.0.2 renderer=Cycles samples=64 frames=8",
        "2026-09-29 11:02:11 turntable figurine=blair-kraken "
        "engine=Blender-4.0.2 renderer=Cycles samples=64 frames=8",
        "2026-09-29 11:02:40 stitch ffmpeg -> turntables/<name>.mp4 + .gif",
        "2026-09-29 11:03:40 note: chris asked about Runware video-gen "
        "for his — blocked, imagegen wallet empty; fallback is the same "
        "Blender pipeline",
    ]),
}


async def _system(ctx, framing: str) -> str:
    """Full production workspace prompt (its CRITICAL block now teaches
    final-text delivery — 'your FINAL text output is your reply') +
    clock + framing."""
    from pathlib import Path

    from server.services.prompt_assembler import (
        load_workspace_prompt, local_now_prompt_line,
        terminal_contract_tail,
    )
    base = await load_workspace_prompt(
        Path(ctx.settings.harness.workspace_dir), db=ctx.db)
    # WhatsApp-shaped turns end with the terminal contract in production
    # (build_chat_messages); fixtures ride the same block.
    return "\n\n".join(
        p for p in (base, local_now_prompt_line(), framing,
                    terminal_contract_tail()) if p)


def _make_send_tool(state: dict):
    """The production send tool's exact surface (whatsapp_bridge_service/
    _service.py) with a recording handler — name, description, params and
    required=[] all match the NEW progress/media framing, so tool salience
    is production-faithful."""
    from server.services.tools import Tool

    async def _send(text: str = "", media_path: str = "") -> str:
        state["sends"].append(text)
        return "Message sent (request_id=eval-mock)"

    return Tool(
        name="send_whatsapp_message",
        description=(
            "Send a WhatsApp message to this conversation right now — BEFORE you finish. "
            "Use it for a brief progress update while you work, or for a reply with media attached "
            "(media_path; text is then the caption and may be empty for media-only sends). "
            "Your final text reply is delivered automatically — do not use this tool to repeat it."
        ),
        parameters={
            "text": {"type": "string", "description": "The message text to send (used as caption when media_path is provided; optional when sending media only)."},
            "media_path": {"type": "string", "description": "Optional path to an image or media file, relative to the workspace directory."},
        },
        required=[],
        handler=_send,
    )


@eval_case(
    id="bg_silence_no_reply_marker",
    category="bg_delivery",
    description="The silence primitive: an explicit ask not to reply must "
                "finish with the exact NO_REPLY marker — final-text "
                "delivery means silence is now an output the model must "
                "produce, not a tool call it must skip.",
    structural_checks=[
        StructuralCheck(kind="context_flag",
                        params={"key": "silence_marker"}),
        StructuralCheck(kind="response_not_contains", params={
            "terms": ["haha", "noted, no reply"]}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "The last INPUT MESSAGE explicitly asks Bob NOT to reply — "
            "the only correct finish is the exact text NO_REPLY (a "
            "decorated variant like '[NO_REPLY — as asked]' is fine). "
            "CORRECT: final text is the NO_REPLY marker. WRONG: any "
            "substantive reply (an acknowledgment, a joke, 'noted!'); "
            "empty/blank output that isn't the marker (the marker is the "
            "contract, not mere emptiness)."
        ),
    ),
)
async def bg_silence_no_reply_marker(ctx):
    from server.repositories.conversations import ConversationRepository
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.tool_registry import build_common_tools

    session_key = "eval:bg_delivery:silence"
    await ConversationRepository(ctx.db).ensure(session_key)

    try:
        messages = [
            {"role": "system", "content": await _system(ctx, _GROUP_FRAMING)},
            {"role": "user", "content": "[Simon] Bob — don't reply to this one, just logging the fixture times for the club."},
        ]

        state = {"sends": []}
        crowd = build_common_tools(ctx, session_key=session_key,
                                   is_trusted=True, contact_id=None)
        tools = ([_make_send_tool(state)]
                 + make_shadow_surface(
                     crowd, exclude={"send_whatsapp_message", "bash"}))

        response = await LLMDispatchService(ctx).chat_with_tools(
            messages, tools, model=pinned_model(),
            call_category="eval", session_key=session_key)

        from server.services.dispatch_runner import is_no_reply
        return {
            "response": response,
            "context": {"tool_calls": extract_tool_calls(messages),
                        "silence_marker": is_no_reply(response)},
            "input_messages": messages,
        }
    finally:
        pass


@eval_case(
    id="bg_flight_answers_in_final_text",
    category="bg_delivery",
    description="The detached flight (delivery note injected between "
                "rounds, as production detaches) investigates via bash and "
                "answers in its FINAL text — no send call required; the "
                "supervisor delivers it. Pins the 2026-09-29 incident "
                "shape under the new contract.",
    structural_checks=[
        StructuralCheck(kind="tool_call_made",
                        params={"tool_name": "bash"}),
        StructuralCheck(kind="context_flag",
                        params={"key": "grounded_answer"}),
        StructuralCheck(kind="min_length", params={"min_length": 5}),
    ],
    judge_criteria=JudgeCriteria(
        extra_instructions=(
            "Mike asks whether the turntables made earlier today used "
            "Runware video-gen. The earlier exchange is NOT in context; "
            "the render logs ARE on disk (bash reaches "
            "scratch/turntable-render.log, which shows Blender 4.0.2 "
            "Cycles frame renders and a note that Runware was blocked). "
            "A '[System note]' may appear mid-transcript after tool calls "
            "— it is channel plumbing stating the detached delivery "
            "contract, not a user message. CORRECT: bash calls visible in "
            "INPUT MESSAGES to check the logs, and the FINAL TEXT is the "
            "plain answer (no Runware — Blender frame renders); calling "
            "send_whatsapp_message with a brief progress update first is "
            "fine but not required. WRONG: answering from assumption with "
            "no check; claiming no record without having looked; leaving "
            "the final text empty or meta ('I will now check…') — under "
            "final-text delivery an empty or meta ending delivers "
            "nothing useful."
        ),
    ),
)
async def bg_flight_answers_in_final_text(ctx):
    from server.repositories.conversations import ConversationRepository
    from server.services.backburner import delivery_note
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.tool_registry import build_common_tools
    from server.services.tools import Tool

    session_key = "eval:bg_delivery:flight"
    await ConversationRepository(ctx.db).ensure(session_key)

    tree_cleanup = None
    try:
        messages = [
            {"role": "system", "content": await _system(ctx, _DM_FRAMING)},
            # The real lead-in from that night: "earlier today" is concrete.
            {"role": "user", "content": "Show me one of the existing turntables"},
            {"role": "assistant", "content": "[sent turntables/mike-shark.mp4]"},
            {"role": "user", "content": _QUESTION},
        ]

        state = {"sends": []}
        bash, tree_cleanup = make_planted_bash(_RENDER_LOGS)
        # Production timing: the backburner appends the delivery note to
        # the flight's live messages at detach — between tool rounds, not
        # upfront. Mirror that: the note rides in after the flight's FIRST
        # bash call, imported from production so the fixture tracks the
        # wording it pins.
        _inner = bash.handler
        _armed = {"note": True}

        async def _bash_with_note(**kwargs):
            out = await _inner(**kwargs)
            if _armed["note"]:
                _armed["note"] = False
                messages.append({"role": "user", "content": delivery_note(
                    "b601750c", "send_whatsapp_message")})
            return out

        bash = Tool(name=bash.name, description=bash.description,
                    parameters=bash.parameters, required=bash.required,
                    handler=_bash_with_note)
        crowd = build_common_tools(ctx, session_key=session_key,
                                   is_trusted=True, contact_id=None)
        tools = ([_make_send_tool(state), bash]
                 + make_shadow_surface(
                     crowd, exclude={"bash", "send_whatsapp_message"}))

        response = await LLMDispatchService(ctx).chat_with_tools(
            messages, tools, model=pinned_model(),
            call_category="eval", session_key=session_key)

        answered = "blender" in (response or "").lower()
        return {
            "response": response,
            "context": {"tool_calls": extract_tool_calls(messages),
                        "grounded_answer": answered},
            "input_messages": messages,
        }
    finally:
        if tree_cleanup is not None:
            await tree_cleanup()
