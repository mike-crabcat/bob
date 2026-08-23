"""Tier 0 structural gates: addressed detection (Bob3 Phase III).

Structural detection bypasses the model probe entirely. The name-variant
grammar is deliberately enumerated (and golden-tested) rather than fuzzy:
every pattern here is a hard "the sender is talking to Bob" signal.

Detection sources, strongest first:
1. DM — everything in a DM is addressed to Bob.
2. Explicit @mention of Bob's JID in the message's mention list.
3. Reply to one of Bob's messages.
4. Name variants in text (word-boundary, case-insensitive):
   - vocative prefix:  "Bob, ..." / "Bob: ..." / "bob ..." (first word)
   - greeting + name:  "hey bob", "hi bob", "ok bob", "yo bob", "oi bob"
   - @name mention:    "@bob" anywhere
   - vocative suffix:  "..., bob" / "... bob?" / "... bob!" (last word)
   - direct question:  "bob?" anywhere
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_GREETINGS = ("hey", "hi", "hello", "ok", "okay", "yo", "oi", "hej", "thanks", "thank you")


@dataclass
class AddressedResult:
    addressed: bool
    reason: str  # dm | mention_jid | reply_to_bot | name_variant | not_addressed


def _name_variant_patterns(bot_name: str) -> list[re.Pattern]:
    n = re.escape(bot_name)
    greet = "|".join(re.escape(g) for g in _GREETINGS)
    return [
        re.compile(rf"^\s*{n}\s*[,:.!?]", re.IGNORECASE),          # vocative prefix w/ punctuation
        re.compile(rf"^\s*{n}\s+\w", re.IGNORECASE),               # first word, sentence follows
        re.compile(rf"\b(?:{greet})[ ,]+{n}\b", re.IGNORECASE),    # greeting + name
        re.compile(rf"@{n}\b", re.IGNORECASE),                     # @name
        re.compile(rf"[,;]\s*{n}\s*[.!?]*\s*$", re.IGNORECASE),    # vocative suffix
        re.compile(rf"\b{n}\s*\?", re.IGNORECASE),                 # "bob?" direct question
    ]


def detect_addressed(
    text: str,
    *,
    bot_name: str,
    chat_kind: str,
    bot_jid: str | None = None,
    mentioned_jids: tuple[str, ...] | list[str] = (),
    reply_to_bot: bool = False,
) -> AddressedResult:
    if chat_kind != "group":
        return AddressedResult(True, "dm")
    if bot_jid and any(j == bot_jid for j in mentioned_jids):
        return AddressedResult(True, "mention_jid")
    if reply_to_bot:
        return AddressedResult(True, "reply_to_bot")
    if text:
        for pat in _name_variant_patterns(bot_name):
            if pat.search(text):
                return AddressedResult(True, "name_variant")
    return AddressedResult(False, "not_addressed")
