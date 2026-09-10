"""Tool-loop context folding (2026-09-10 diagnosis: prefill amplification).

A tool-heavy dispatch re-sends the whole growing transcript on EVERY
iteration — worst observed: ~30 iterations × ~30k-token prefix ≈ 1.2M
cumulative prompt tokens and 3-10 minute turns, because even cached prefill
is throughput-bound. Two levers, both gated so small turns are byte-identical
to today:

1. ``fold_aged_tool_outputs`` — in-place: text tool results that are big
   AND older than the last ``keep_last`` get folded to head+tail with an
   elision marker naming how much dropped and how to get it back (re-run:
   the call itself always survives verbatim above the result). Never folds
   media (media rides synthetic user blocks, not function_call_output),
   small results (grounding numbers live there), or recent results.
   The model's own assistant messages — its interpretations of those
   outputs — are never touched: conclusions survive, stale raw detail
   costs one re-run call to recover.

2. ``iteration_view`` — per-request, non-destructive: when the incoming
   chat history is big, intermediate iterations send system + the last
   ``history_keep`` history messages + the full loop transcript. The
   canonical list is untouched (the caller's history stays intact for
   persistence); only the wire copy is trimmed.
"""

from __future__ import annotations

import json
from typing import Any

# Stable tag so folds are idempotent and greppable in logs/messages_json.
ELISION_TAG = "[…tool-loop fold:"
ELISION_MARK = ELISION_TAG + " {dropped} chars elided — re-run the call to see the full output…]"


def _foldable(m: dict[str, Any]) -> bool:
    return (m.get("type") == "function_call_output"
            and isinstance(m.get("output"), str))


def fold_aged_tool_outputs(
    messages: list[dict[str, Any]],
    *,
    keep_last: int = 4,
    size_threshold: int = 4000,
    head_chars: int = 1500,
    tail_chars: int = 500,
) -> int:
    """Fold oversized aged text tool results in place. Returns chars dropped.

    Idempotent: already-folded outputs (ELISION_TAG present) are skipped, so
    the loop can call this after every tool round cheaply.
    """
    candidates = [i for i, m in enumerate(messages)
                  if _foldable(m)
                  and len(m["output"]) > size_threshold
                  and ELISION_TAG not in m["output"]]
    aged = candidates[:-keep_last] if keep_last > 0 else candidates
    dropped = 0
    for i in aged:
        out = messages[i]["output"]
        keep = head_chars + tail_chars
        marker = ELISION_MARK.format(dropped=max(0, len(out) - keep))
        messages[i]["output"] = (
            out[:head_chars] + "\n" + marker + "\n" + out[-tail_chars:])
        dropped += len(out) - len(messages[i]["output"])
    return dropped


def history_chars(messages: list[dict[str, Any]], base_len: int) -> int:
    """Cheap size estimate of the pre-loop history block."""
    return sum(len(json.dumps(m, default=str)) for m in messages[:base_len])


def iteration_view(
    messages: list[dict[str, Any]],
    base_len: int,
    *,
    history_keep: int = 20,
) -> list[dict[str, Any]]:
    """The wire copy for one iteration: system + recent history + transcript.

    Non-destructive — returns a new list; ``messages`` is never trimmed. Only
    meaningful when the caller has already decided the history is big enough
    to warrant a view (see history_chars); small histories should pass the
    list itself.
    """
    if base_len <= history_keep + 1:
        return messages
    head = messages[:1] if messages and messages[0].get("role") == "system" \
        else []
    return head + messages[max(0, base_len - history_keep):base_len] \
        + messages[base_len:]
