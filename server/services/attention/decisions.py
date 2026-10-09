"""Attention probe on a DECISIONS model (2026-10-10).

Decisions models (OpenAI's GPT-6 Luna Decisions, TypeSafe Jev) don't
generate text: they answer typed questions over a ``state`` object with
calibrated probabilities, via OpenRouter's POST /api/alpha/decisions. The
probe is one ``choice`` question (ACT / WAIT / STAND_DOWN) plus, when the
reaction tier is on, a second ``choice`` for the clip. Bench 2026-10-10:
Luna Decisions 10/10 golden, 0.41s p50 vs ~1.6s for luna via chat.

``decide`` returns None — and the caller falls back to the chat probe —
on any HTTP/parse error, a refusal, or a verdict below the confidence floor
(BOB_PROBE_DECISIONS_MIN_CONFIDENCE, default 0.6). The endpoint is alpha:
its shape may change, so failure must always degrade, never block.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
TIMEOUT_S = 8.0

CRITERIA = {
    "STAND_DOWN": (
        "Default. {bot} was not spoken to and is not mid-exchange: banter, jokes or "
        "reactions between others; messages addressed to another specific person; "
        "{bot} spoke earlier but the conversation moved on; pure social exchange with "
        "nothing {bot} can answer or act on. When ambiguous, STAND_DOWN."),
    "ACT": (
        "{bot} should respond now: the batch continues an active exchange {bot} was part "
        "of in the last few turns (callbacks, follow-ups, adding info to what {bot} "
        "just said); {bot} was asked something recently that is still unanswered; or a "
        "question/request aimed at the GROUP that {bot} can answer or do (he is the "
        "group's assistant — it need not name him). Third-party names inside a "
        "question are topics, not addressees."),
    "WAIT": (
        "Defer and re-check shortly: the sender appears mid-thought (fragments, "
        "trailing '...'), or several people are actively deciding/discussing and the "
        "thread hasn't settled yet."),
}


def is_decisions_model(model: str | None) -> bool:
    m = (model or "").strip().lower()
    return m.endswith("-decisions") or "typesafe/jev" in m


def min_confidence() -> float:
    try:
        return float(os.environ.get("BOB_PROBE_DECISIONS_MIN_CONFIDENCE", "0.6"))
    except ValueError:
        return 0.6


def build_request(model: str, context_text: str, bot_name: str,
                  reactions: bool) -> dict[str, Any]:
    from server.services.attention.tier2 import REACTION_CLIPS, probe_system_prompt
    policy = probe_system_prompt(bot_name).split("\nRespond with ONLY")[0]
    questions: dict[str, Any] = {
        "decision": {
            "type": "choice",
            "instructions": (f"Group-chat attention gate for the chatbot {bot_name}: "
                             f"given the policy and the pending batch, should "
                             f"{bot_name} respond?"),
            "criteria": {k: v.format(bot=bot_name) for k, v in CRITERIA.items()},
        }
    }
    if reactions:
        clips = {name: cue for name, cue in REACTION_CLIPS.items()}
        clips["none"] = ("No clip — the default. Only a rare perfect beat (a win, a "
                         "glorious failure, real drama) earns a clip; most moments get none.")
        questions["react"] = {
            "type": "choice",
            "instructions": (f"If {bot_name} stays quiet, should he post one short "
                             "reaction clip instead? Usually none."),
            "criteria": clips,
        }
    return {"model": model,
            "state": {"gate_policy": policy, "group_chat_batch": context_text},
            "questions": questions}


def parse_answers(out: dict[str, Any], *, reactions: bool,
                  floor: float) -> dict[str, Any] | None:
    """Map a decisions response onto the probe result, or None to fall back."""
    answers = out.get("answers") or {}
    a = answers.get("decision") or {}
    choice = str(a.get("choice") or "").upper()
    if choice not in ("ACT", "WAIT", "STAND_DOWN"):
        return None
    conf = float(a.get("confidence") or 0.0)
    if conf < floor:
        return None
    probs = a.get("probabilities") or {}
    react = None
    if reactions and choice == "STAND_DOWN":
        r = answers.get("react") or {}
        pick = str(r.get("choice") or "").strip().lower()
        if pick and pick != "none" and float(r.get("confidence") or 0) >= floor:
            react = pick
    reason = "decisions: " + " ".join(
        f"{k}={float(v):.2f}" for k, v in probs.items()) + f" (conf {conf:.2f})"
    return {"decision": choice, "react": react, "reason": reason, "confidence": conf}


async def decide(ctx: Any, model: str, context_text: str, *, bot_name: str = "Bob",
                 session_key: str = "") -> dict[str, Any] | None:
    import httpx

    from server.services.attention.tier2 import REACTION_CLIPS, reactions_enabled
    key = getattr(getattr(ctx.settings, "openrouter", None), "api_key", "") or ""
    if not key:
        logger.warning("probe decisions: no OpenRouter key — falling back")
        return None
    reactions = reactions_enabled()
    body = build_request(model, context_text, bot_name, reactions)
    t0 = time.monotonic()
    out: dict[str, Any] = {}
    err: str | None = None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.post(DECISIONS_URL, json=body,
                                  headers={"Authorization": f"Bearer {key}"})
        out = r.json() if r.content else {}
        if r.status_code != 200 or "error" in out:
            err = f"HTTP {r.status_code}: {str(out.get('error') or out)[:300]}"
    except Exception as exc:  # noqa: BLE001 — degrade, never block
        err = f"{type(exc).__name__}: {exc}"
    latency = time.monotonic() - t0

    result = None if err else parse_answers(out, reactions=reactions, floor=min_confidence())
    if result and result.get("react") not in (None, *REACTION_CLIPS):
        result["react"] = None
    await _log(ctx, model=model, session_key=session_key, body=body, out=out,
               latency=latency, err=err,
               fell_back=result is None and err is None)
    if err:
        logger.warning("probe decisions failed for %s (%s) — falling back", session_key, err)
    elif result is None:
        logger.info("probe decisions low-confidence/unknown for %s — falling back: %s",
                    session_key, json.dumps((out.get("answers") or {}).get("decision"))[:200])
    else:
        logger.info("tier2: %s for %s (%s%s) [%.2fs]", result["decision"], session_key,
                    result["reason"], f", react: {result['react']}" if result["react"] else "",
                    latency)
    return result


async def _log(ctx: Any, *, model: str, session_key: str, body: dict, out: dict,
               latency: float, err: str | None, fell_back: bool) -> None:
    try:
        from server.services.llm_dispatch import _record_log
        usage = out.get("usage") or {}
        await _record_log(
            ctx.db, provider="openrouter", model=out.get("model") or model,
            call_category="attention_probe", session_key=session_key or None,
            system_prompt=body["state"]["gate_policy"],
            user_message=body["state"]["group_chat_batch"],
            messages_json=json.dumps(body),
            response_text=json.dumps(out.get("answers") or {}) + (
                " [below confidence floor — fell back to chat probe]" if fell_back else ""),
            latency_seconds=latency,
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
            status="failed" if err else "completed",
            error_message=err,
            generation_id=out.get("id"))
    except Exception:
        logger.debug("probe decisions: log write failed", exc_info=True)
