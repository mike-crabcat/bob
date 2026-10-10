"""Tool-calling mechanics in a real turn: the session agenda round-trip,
and long tool loops with context folding engaged (the capability the
2026-09-10 folding change could regress)."""

from __future__ import annotations

from server.evals.case import Check
from server.evals.registry import eval_case

_AGENDA = "## Task List\n1. Calculate 1+1 and report the result\n2. Summarize the answer in a sentence\n"


@eval_case(
    id="tool_calling_update_agenda",
    category="tool_calling",
    description="On an email thread whose agenda lists tasks, Bob completes "
                "task 1 and marks it done with update_agenda (email turns render "
                "the agenda; WhatsApp turns don't — finding 2026-10-08).",
    checks=[Check("tool_call_made", {"tool_name": "update_agenda"}),
            Check("flag", {"key": "agenda_task1_done"})],
    rubric=["Bob's reply tells Mike that 1+1 = 2."],
    context="The thread's agenda (rendered into the email turn's prompt) lists "
            "two tasks; task 1 is 'Calculate 1+1 and report the result'.",
)
async def tool_calling_update_agenda(w):
    from server.services.email_polling_service import _build_session_key
    from server.services.session_agenda_service import SessionAgendaService
    thread = "thread-eval-agenda"
    key = _build_session_key(thread)
    svc = SessionAgendaService(w.ctx)
    await svc.set_agenda(key, _AGENDA)
    await w.email("mike@example.org", "Mike Cleaver", "Agenda check",
                  "Please complete task 1 on this thread's agenda and update the "
                  "agenda to show it's done.", thread=thread)
    after = (await svc.get_agenda(key) or "").lower()
    line1 = next((l for l in after.splitlines() if "1+1" in l), "")
    w.flag("agenda_after", after[:400])
    w.flag("agenda_task1_done", after != _AGENDA.lower() and (
        not line1 or any(m in line1 for m in ("done", "complete", "✓", "✅", "~~", "[x]"))))
    mail = [d for d in w.deliveries if d["kind"].startswith("email")]
    return {"response": (str(mail) + "\n" + w.reply()).strip()}


HEAD_CODE, STEP3_CODE, TAIL_WORD = "417729", "8302", "ZEBRALIGHT"


@eval_case(
    id="tool_loop_folding_long_loop",
    category="tool_calling",
    description="A 6-call tool loop with folding engaged (aged results folded "
                "to head/tail excerpts): the answer still cites the head "
                "number, the tail marker and an aged step's number.",
    checks=[Check("flag", {"key": "folding_engaged"}),
            Check("response_contains", {"terms": [HEAD_CODE]}),
            Check("response_contains", {"terms": [STEP3_CODE]}),
            Check("response_contains", {"terms": [TAIL_WORD]})],
    rubric=[f"Bob reports exactly HEAD={HEAD_CODE}, STEP3={STEP3_CODE} and "
            f"TAIL={TAIL_WORD} (close-but-wrong digits fail)."],
    context="This world folds tool results over 2000 chars, keeping only the "
            "newest verbatim (head 400 / tail 200 chars).",
    timeout_seconds=420,
)
async def tool_loop_folding_long_loop(w):
    from server.config import ToolLoopSettings
    w.ctx.settings.tool_loop = ToolLoopSettings(
        folding_enabled=True, fold_keep_last=1, fold_size_threshold=2000,
        fold_head_chars=400, fold_tail_chars=200,
        history_view_keep=10, history_view_trigger_chars=2000)
    ledger = (f"LEDGER SNAPSHOT\nopening balance code: {HEAD_CODE}\nrow: "
              + "x" * 90 + "\n"
              + "\n".join(f"entry {i:04d} " + "y" * 80 for i in range(80))
              + f"\nFINAL MARKER {TAIL_WORD}\n")
    w.files({"scratch/ledger.txt": ledger,
             "scratch/step.sh": (
                 "#!/bin/bash\nn=$1\nif [ \"$n\" = 3 ]; then code="
                 f"{STEP3_CODE}; else code=$((1000 + n)); fi\n"
                 "echo \"STEP $n COMPLETE — code $code\"\n"
                 "for i in $(seq -w 0 34); do echo \"tick $i $(printf 'z%.0s' {1..60})\"; done\n")})
    turns = await w.inbound(
        "Mike", "Run `cat scratch/ledger.txt` once, then run `bash "
        "scratch/step.sh N` for N = 1, 2, 3, 4, 5 — five separate bash calls. "
        "Then reply in exactly this format and nothing else: HEAD=<code> "
        "STEP3=<code> TAIL=<word> — the ledger's opening balance code, step 3's "
        "code, and the ledger's FINAL MARKER word.")
    from server.services.tool_loop_folding import ELISION_TAG
    folded = sum(1 for t in turns for m in t.messages
                 if isinstance(m, dict) and m.get("type") == "function_call_output"
                 and isinstance(m.get("output"), str) and ELISION_TAG in m["output"])
    w.flag("folding_engaged", folded >= 2)
