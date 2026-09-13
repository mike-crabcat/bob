"""Tolerant JSON extraction from LLM output.

Models behind OpenRouter (GLM-5.3 especially) intermittently wrap JSON in
prose or fences, or truncate it mid-string when they hit the token budget.
Every consumer that parses a structured block out of a completion should go
through :func:`parse_llm_json` instead of ``json.loads`` — the rescue ladder
here recovers the fence/prose cases exactly and repairs truncated tails by
closing unterminated containers, which covers the common failure shapes seen
in the 2026-09-12 memory review (reconciliation finals, claim-router probes).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def _strip_fence(text: str) -> str:
    """Drop ```json fences and surrounding whitespace."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s.strip("`")
        s = s.rsplit("```", 1)[0]
    return s.strip()


def _candidates(s: str):
    """Yield plausible JSON substrings, most-intact first."""
    yield s
    first = min((p for p in (s.find("{"), s.find("[")) if p >= 0), default=-1)
    if first < 0:
        return
    for closer in ("}", "]"):
        last = s.rfind(closer)
        if last > first:
            yield s[first : last + 1]
    yield s[first:]


def _close_json(candidate: str) -> str | None:
    """Repair a truncated JSON document by closing open containers.

    Cuts at the last structurally complete token (closed string, closed
    bracket), heals a dangling separator, then appends the missing closers
    from the bracket stack. Returns None when nothing can be salvaged.
    """
    stack: list[str] = []
    in_str = False
    esc = False
    last_safe = -1
    for i, ch in enumerate(candidate):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                last_safe = i + 1
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            last_safe = i + 1
    if not stack and not in_str:
        return candidate  # already balanced — caller's failure was elsewhere

    prefix = candidate[: last_safe if last_safe >= 0 else 0].rstrip()
    # Heal the tail so the closers below produce valid JSON.
    if prefix.endswith(","):
        prefix = prefix[:-1]
    elif prefix.endswith(":"):
        prefix += " null"
    elif prefix.endswith('"') and stack and stack[-1] == "{":
        prefix += ": null"  # completed string is a dangling object key
    for opener in reversed(stack):
        prefix += "}" if opener == "{" else "]"
    return prefix


def parse_llm_json(text: str) -> dict | list | None:
    """Parse a JSON object/array out of LLM output, tolerating fences, prose
    padding, and truncation. Returns None when nothing parses."""
    if not isinstance(text, str) or not text.strip():
        return None
    s = _strip_fence(text)
    for candidate in _candidates(s):
        for attempt in (candidate, _close_json(candidate)):
            if not attempt:
                continue
            try:
                parsed = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, (dict, list)):
                if attempt is not candidate:
                    logger.debug("Recovered truncated JSON from LLM output")
                return parsed
    return None


def parse_llm_verdict(text: str, options: dict[str, str]) -> str | None:
    """Extract a verdict word from LLM output.

    Tries JSON first (``{"verdict": "X"}``), then falls back to scanning for
    any option keyword. ``options`` maps the keyword as it appears in output
    (case-insensitive match) to the value to return; the first matching key
    in dict order wins, so callers should list their safe default first.
    Returns None when no option appears.
    """
    parsed = parse_llm_json(text)
    if isinstance(parsed, dict):
        for v in parsed.values():
            if isinstance(v, str):
                hit = _match_option(v, options)
                if hit:
                    return options[hit]
    hit = _match_option(text or "", options)
    return options[hit] if hit else None


def _match_option(text: str, options: dict[str, str]) -> str | None:
    upper = text.upper()
    for key in options:
        if key.upper() in upper:
            return key
    # Truncation fallback: the tail of the text may be a cut-off option
    # word ('{"verdict": "RELEV'). Only fire on a sufficiently long final
    # token with exactly one option extension, so prose like "I think…"
    # never matches.
    tail = upper.rstrip('"\'}] ')
    word = ""
    for ch in reversed(tail):
        if ch.isalnum():
            word = ch + word
        else:
            break
    if len(word) >= 4:
        hits = [k for k in options if k.upper().startswith(word)]
        if len(hits) == 1:
            return hits[0]
    return None
