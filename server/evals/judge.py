"""Scoring: mechanical checks first, then the checklist judge.

The old judge averaged correctness/relevance/completeness into one number
against a prose rubric and cut at 0.7 — a fuzzy score over a buried
standard. Now every rubric item is a binary statement judged on its own,
with a reason; the trial passes only if all items pass. The judge model
defaults to gpt-6-astra (2026-09-28: nano false-negatived verbatim-correct
quotes) at temperature 0, and its agreement with hand labels is measured
by ``bob eval judge-calibrate``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from server.evals.case import Check, CheckResult, EvalCase, JudgeResult, RubricVerdict

logger = logging.getLogger(__name__)

DEFAULT_JUDGE_MODEL = "gpt-6-astra"


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def run_checks(checks: list[Check], *, response: str,
               tool_calls: list[dict[str, Any]], flags: dict[str, Any],
               delivered: dict[str, list[str]], any_delivery: bool
               ) -> list[CheckResult]:
    return [_check(c, response, tool_calls, flags, delivered, any_delivery)
            for c in checks]


def _ok(c: Check) -> CheckResult:
    return CheckResult(check=c, passed=True)


def _bad(c: Check, detail: str) -> CheckResult:
    return CheckResult(check=c, passed=False, detail=detail)


def _check(c: Check, response: str, calls: list[dict[str, Any]],
           flags: dict[str, Any], delivered: dict[str, list[str]],
           any_delivery: bool) -> CheckResult:
    p = c.params
    names = [tc.get("name", "?") for tc in calls]
    text = (response or "").strip()
    low = text.lower()
    k = c.kind
    if k == "min_length":
        n = p.get("min_length", 0)
        return _ok(c) if len(text) >= n else _bad(c, f"{len(text)} chars (min {n})")
    if k == "max_length":
        n = p.get("max_length", 10**9)
        return _ok(c) if len(text) <= n else _bad(c, f"{len(text)} chars (max {n})")
    if k == "response_contains":
        terms = p.get("terms", [])
        return (_ok(c) if any(t.lower() in low for t in terms)
                else _bad(c, f"none of {terms} in response"))
    if k == "response_not_contains":
        hit = [t for t in p.get("terms", []) if t.lower() in low]
        return _bad(c, f"forbidden term(s): {hit}") if hit else _ok(c)
    if k == "tool_call_made":
        t = p.get("tool_name", "")
        return _ok(c) if t in names else _bad(c, f"{t} not called (calls: {names})")
    if k == "any_tool_call":
        want = p.get("tool_names", [])
        if not want:
            return _bad(c, "any_tool_call requires tool_names")
        return (_ok(c) if any(n in want for n in names)
                else _bad(c, f"none of {want} called (calls: {names})"))
    if k == "no_tool_call":
        banned = p.get("tool_names", [])
        if not banned:
            return _bad(c, "no_tool_call requires tool_names")
        fired = sorted({n for n in names if n in banned})
        return _bad(c, f"forbidden call(s): {fired}") if fired else _ok(c)
    if k in ("tool_call_like", "no_tool_call_like"):
        pat = p.get("pattern", "")
        hit = [n for n in names if pat and pat in n]
        if k == "tool_call_like":
            return _ok(c) if hit else _bad(c, f"no tool matching {pat!r} (calls: {names})")
        return _bad(c, f"forbidden call(s): {hit}") if hit else _ok(c)
    if k == "tool_call_args":
        t = p.get("tool_name", "")
        need = p.get("arg_contains", [])
        cands = [tc for tc in calls if tc.get("name") == t]
        if not cands:
            return _bad(c, f"{t} not called (calls: {names})")
        if any(all(str(s) in str(tc.get("arguments", "")) for s in need)
               for tc in cands):
            return _ok(c)
        return _bad(c, f"{t} called but no call has all of {need}")
    if k in ("flag", "context_flag"):
        key = p.get("key", "")
        if not key:
            return _bad(c, "flag requires key")
        return (_ok(c) if flags.get(key)
                else _bad(c, f"flag {key!r} is falsy ({flags.get(key)!r})"))
    if k in ("delivered_to", "not_delivered_to"):
        chat = p.get("chat", "")
        texts = delivered.get(chat, [])
        terms = [t.lower() for t in p.get("terms", [])]
        hits = [x for x in texts if not terms or any(t in x.lower() for t in terms)]
        if k == "delivered_to":
            return (_ok(c) if hits else _bad(
                c, f"nothing{' matching ' + str(terms) if terms else ''} "
                   f"delivered to {chat} (got {len(texts)} message(s))"))
        return (_bad(c, f"delivered to {chat}: {[h[:80] for h in hits]}")
                if hits else _ok(c))
    if k == "no_reply":
        from server.services.dispatch_runner import is_no_reply
        if any_delivery:
            return _bad(c, "something was delivered")
        return _ok(c) if is_no_reply(text) else _bad(c, f"final text {text[:60]!r} is not NO_REPLY")
    if k == "json_valid":
        try:
            json.loads(text)
            return _ok(c)
        except json.JSONDecodeError as e:
            return _bad(c, str(e))
    return _bad(c, f"unknown check kind {k!r}")


# --------------------------------------------------------------------------
# checklist judge
# --------------------------------------------------------------------------

_JUDGE_PROMPT = """\
You are grading one trial of an AI agent ("Bob") against a checklist.
Bob is a WhatsApp-based assistant; the transcript below is what really
happened: his prompt (system prompt elided in the middle), the
conversation, every tool call he made with its real output, his final
text, and what was actually delivered to people.

EVIDENCE RULES
- "[Bob called tool]" lines are actions Bob actually took. Count them as
  done even if his text doesn't mention them; never credit an action that
  has no such line.
- Under final-text delivery, Bob's FINAL TEXT on a human-message turn is
  his reply (it shows up under DELIVERED). Steering and wake turns do NOT
  deliver their final text — only sends do. Judge what people received
  from the DELIVERED section.
- "NO_REPLY" as final text is deliberate silence.
- Judge each item independently and literally. Do not fail an item for
  something another item covers. Do not reward or penalise style unless
  an item asks about it.

CASE
{description}

FACTS FOR THE GRADER
{context}

TRANSCRIPT
{transcript}

TEXT UNDER REVIEW (Bob's reply for this case)
{response}

CHECKLIST — each item is a statement that must be TRUE for the trial to pass:
{items}

Reply with JSON only:
{{"items": [{{"n": 1, "verdict": "pass" | "fail", "why": "one sentence citing the evidence"}}, ...]}}"""


async def judge(ctx: Any, case: EvalCase, *, transcript: str, response: str,
                judge_model: str | None = None, judge_effort: str | None = None,
                rubric: list[str] | None = None) -> JudgeResult:
    from server.services.llm_dispatch import LLMDispatchService

    items = rubric if rubric is not None else case.rubric
    prompt = _JUDGE_PROMPT.format(
        description=case.description,
        context=case.context or "(none)",
        transcript=transcript[-60000:],
        response=response or "(empty)",
        items="\n".join(f"{i}. {item}" for i, item in enumerate(items, 1)))
    t0 = time.monotonic()
    try:
        raw = await LLMDispatchService(ctx).prompt(
            [{"role": "user", "content": prompt}],
            call_category="eval_judge", temperature=0.0,
            model=judge_model or DEFAULT_JUDGE_MODEL,
            reasoning_effort=judge_effort)
        data = _parse_json(raw)
        by_n = {int(v.get("n", 0)): v for v in data.get("items", [])}
        verdicts = []
        for i, item in enumerate(items, 1):
            v = by_n.get(i)
            if v is None:
                return JudgeResult(error=f"judge skipped item {i}",
                                   seconds=time.monotonic() - t0)
            verdicts.append(RubricVerdict(
                item=item, passed=str(v.get("verdict", "")).lower() == "pass",
                why=str(v.get("why", ""))[:400]))
        return JudgeResult(verdicts=verdicts, seconds=time.monotonic() - t0)
    except Exception as e:
        logger.warning("judge failed for %s: %s", case.id, e)
        return JudgeResult(error=str(e)[:300], seconds=time.monotonic() - t0)


def _parse_json(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"judge returned no JSON: {raw[:120]!r}")
    return json.loads(raw[start:end + 1])
