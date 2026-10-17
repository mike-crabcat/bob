"""Reply shape: one answer, said once (2026-10-16 Bob cast double sends).

Under final-text delivery the final text is the reply and the send tool is
for media and long-work progress. Luna posted media and then restated its
caption as the final text in 86–100% of media turns ("Recreated and
posted…", "Glow-stick edition sent."), and announced quick work before
doing it. These pin the shape: a media answer is one message, quick work
gets no opener — and a genuinely new fact after media still gets said.
"""

from __future__ import annotations

from server.evals.case import Check
from server.evals.cases._kit import noise_png
from server.evals.registry import eval_case


def _shape_flags(w, chat: str = "Mike") -> None:
    sent = [d for d in w.deliveries
            if d["kind"].startswith("whatsapp_send")
            and d.get("chat_id") == w.people[chat].jid]
    w.flag("media_sent", any(d.get("file_path") for d in sent))
    w.flag("single_message", len(sent) == 1)


@eval_case(
    id="media_answer_said_once",
    category="reply_shape",
    description="Asked for an image, Bob sends it with a caption and stops — "
                "no follow-up text confirming that he sent it.",
    checks=[Check("flag", {"key": "media_sent"}),
            Check("flag", {"key": "single_message"})],
)
async def media_answer_said_once(w):
    w.files({"generated-images/night-watch-poster.png": noise_png(96, seed=11)})
    await w.inbound("Mike", "Send me the night watch poster — it's in generated-images.")
    _shape_flags(w)


@eval_case(
    id="quick_work_no_opener",
    category="reply_shape",
    description="A quick lookup gets one answer — no 'I'll check…' progress "
                "message ahead of it.",
    checks=[Check("flag", {"key": "single_message"}),
            Check("tool_call_made", {"tool_name": "bash"})],
    rubric=["Bob's reply lists the items actually in notes/todo.md (fix gutter, "
            "book dentist) and invents none."],
)
async def quick_work_no_opener(w):
    w.files({"notes/todo.md": "- fix gutter\n- book dentist\n"})
    await w.inbound("Mike", "What's on my todo list in notes/todo.md?")
    _shape_flags(w)


@eval_case(
    id="media_plus_new_fact_still_said",
    category="reply_shape",
    description="Media plus a second question: the extra answer must still "
                "reach Mike (caption or one follow-up) — the said-once rule "
                "must not swallow new information.",
    checks=[Check("flag", {"key": "media_sent"}),
            Check("flag", {"key": "answered_size"})],
)
async def media_plus_new_fact_still_said(w):
    w.files({"generated-images/night-watch-poster.png": noise_png(96, seed=11)})
    await w.inbound("Mike", "Send me the night watch poster from generated-images, "
                    "and tell me its pixel dimensions.")
    _shape_flags(w)
    said = " ".join(w.sent_to("Mike")).lower().replace(" ", "")
    w.flag("answered_size", "96x96" in said or "96×96" in said or "96by96" in said
           or ("96" in said and "pixel" in said))
