"""Render a world's trial for the judge and the results store.

The judge sees what actually happened, in order: each Bob turn's prompt
(system head + tail — production prompts run ~35k chars, and the tail
carries the terminal contract), the conversation it was given, every tool
call with its output, the final text — then what people actually
RECEIVED (deliveries), which under final-text delivery is the ground
truth for "did Bob say X to Y".
"""

from __future__ import annotations

import json
from typing import Any

from server.evals.world import Turn, World

_SYS_HEAD = 4000
_SYS_TAIL = 4000
_OUT_CAP = 1500


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") in ("input_text", "text", "output_text"):
                    parts.append(str(p.get("text", "")))
                elif p.get("type") in ("input_image", "image_url"):
                    parts.append("[image]")
                elif p.get("type") == "input_video":
                    parts.append("[video]")
            else:
                parts.append(str(getattr(p, "text", "") or ""))
        return "\n".join(x for x in parts if x)
    return str(content or "")


def render_turn(t: Turn, index: int) -> str:
    lines = [f"=== BOB TURN {index} · {t.call_category} · {t.session_key} ==="]
    for m in t.messages:
        if not isinstance(m, dict):
            continue  # SDK reasoning items etc.
        kind = m.get("type")
        if kind == "function_call":
            args = str(m.get("arguments", ""))
            lines.append(f"[Bob called tool] {m.get('name', '?')}({args[:800]})")
            continue
        if kind == "function_call_output":
            lines.append(f"[tool result] {str(m.get('output', ''))[:_OUT_CAP]}")
            continue
        if kind == "reasoning":
            continue
        role = m.get("role", "?")
        text = _content_text(m.get("content"))
        if role == "system" and len(text) > _SYS_HEAD + _SYS_TAIL:
            text = (text[:_SYS_HEAD] + f"\n[… {len(text) - _SYS_HEAD - _SYS_TAIL}"
                    " chars of the production system prompt elided …]\n"
                    + text[-_SYS_TAIL:])
        if m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                lines.append(f"[Bob called tool] {fn.get('name', '?')}"
                             f"({str(fn.get('arguments', ''))[:800]})")
            if not text:
                continue
        lines.append(f"[{role}] {text}")
    if t.error is not None:
        lines.append(f"[turn raised] {t.error!r}")
    else:
        lines.append(f"[FINAL TEXT] {t.result or '(empty)'}")
    return "\n".join(lines)


def render(w: World) -> str:
    # Background-only trials (extraction cases) render their passes in full.
    focus = w.main_turns or w.turns
    out = [render_turn(t, i) for i, t in enumerate(focus, 1)]
    bg = [t for t in w.turns if t.background and t not in focus]
    if bg:
        out.append("=== BACKGROUND PASSES ===")
        for t in bg:
            calls = ", ".join(c["name"] for c in t.tool_calls) or "no tool calls"
            out.append(f"- {t.call_category} on {t.session_key}: {calls}; "
                       f"final: {(t.result or '')[:300]}")
    out.append("=== DELIVERED (what people actually received) ===")
    if not w.deliveries:
        out.append("(nothing was delivered to anyone)")
    for d in w.deliveries:
        if d["kind"].startswith("whatsapp"):
            body = d.get("text") or d.get("caption") or ""
            media = f" [+media {d['file_path']}]" if d.get("file_path") else ""
            out.append(f"→ {w.chat_label(d.get('chat_id', ''))}: {body}{media}")
        else:
            out.append(f"→ {d['kind']}: {json.dumps({k: v for k, v in d.items() if k != 'kind'}, default=str)[:600]}")
    if w.spawns:
        out.append("=== SUBAGENTS SPAWNED ===")
        out += [f"- {s.get('executor')}: {str(s.get('task'))[:600]}" for s in w.spawns]
    if w.bg_jobs:
        out.append("=== BACKGROUND JOBS STARTED ===")
        out += [f"- {n}: {j['command'][:400]}" for n, j in w.bg_jobs.items()]
    if w.calls:
        out.append("=== PHONE CALLS PLACED ===")
        out += [f"- {c}" for c in w.calls]
    return "\n".join(out)
