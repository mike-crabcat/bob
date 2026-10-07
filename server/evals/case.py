"""Eval case types (docs/evals.md → Cases).

A case is a world script plus how to score it:

- ``run(world)`` drives stimuli through production entry points and may
  set flags (``world.flag``) from post-turn state reads. It may return
  ``{"response": …}`` to override which text is judged (default: the
  final text of the last main turn).
- ``checks`` — mechanical assertions, evaluated first. A failed check
  fails the trial without a judge call.
- ``rubric`` — binary checklist items for what checks can't see. Each is
  a statement that must be TRUE of the trial; the judge returns a verdict
  per item and the trial passes only if every item does. ``context``
  carries the facts the judge needs (what's on disk, what the fixture
  returns) — never the expected answer phrased as an item.
- ``tier`` — "discipline" (one-stimulus behaviour pins) or "capability"
  (multi-turn world-state tasks scored on end state).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Check:
    """A mechanical assertion. Kinds and params:

      min_length / max_length — {"min_length": n} / {"max_length": n}
      response_contains       — {"terms": [...]}  (any; case-insensitive)
      response_not_contains   — {"terms": [...]}
      tool_call_made          — {"tool_name": "bash"}
      any_tool_call           — {"tool_names": [...]}
      no_tool_call            — {"tool_names": [...]}
      tool_call_args          — {"tool_name": ..., "arg_contains": [...]}
      tool_call_like / no_tool_call_like — {"pattern": "web_search"} (substring
                                 of the tool name — MCP tools are namespaced)
      flag                    — {"key": "goal_written"}  (truthy world flag)
      delivered_to            — {"chat": "Mike"|"Doom Club", "terms": [...]?}
      not_delivered_to        — {"chat": ..., "terms": [...]?}  (no delivery,
                                 or none containing any term)
      no_reply                — {}  final text is the NO_REPLY marker and
                                 nothing was delivered anywhere
      json_valid              — {}
    """

    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""


# Back-compat name used across older docs/tests.
StructuralCheck = Check


@dataclass
class EvalCase:
    id: str
    category: str
    description: str
    run: Callable[..., Awaitable[Any]]
    checks: list[Check] = field(default_factory=list)
    rubric: list[str] = field(default_factory=list)
    context: str = ""
    tier: str = "discipline"
    timeout_seconds: float = 300.0
    expects_turn: bool = True     # no Bob turn ran → harness_error, never a pass


@dataclass
class CheckResult:
    check: Check
    passed: bool
    detail: str = ""


@dataclass
class RubricVerdict:
    item: str
    passed: bool
    why: str = ""


@dataclass
class JudgeResult:
    verdicts: list[RubricVerdict] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.error and all(v.passed for v in self.verdicts)

    @property
    def score(self) -> float | None:
        if self.error or not self.verdicts:
            return None
        return sum(v.passed for v in self.verdicts) / len(self.verdicts)

    @property
    def reasoning(self) -> str:
        if self.error:
            return f"judge error: {self.error}"
        return "\n".join(f"[{'PASS' if v.passed else 'FAIL'}] {v.item} — {v.why}"
                         for v in self.verdicts)


@dataclass
class Trial:
    """One case × one repeat."""
    case_id: str
    category: str
    repeat_index: int
    passed: bool = False
    failure_class: str | None = None   # harness_error | provider_error |
    #                                    tool_format | empty_response | behavioural
    checks: list[CheckResult] = field(default_factory=list)
    judge: JudgeResult | None = None
    response: str = ""
    transcript: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    deliveries: list[dict[str, Any]] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)
    latency: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    cost_usd: float | None = None
    error: str | None = None
