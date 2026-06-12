"""Data types for the eval framework."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from bob_server.context import AppContext


@dataclass
class StructuralCheck:
    """A single structural assertion on an LLM response.

    Supported kinds and their params:
      json_valid         — no params
      json_schema        — {"required_fields": [...], "array_field": "steps", "min_items": 3}
      field_present      — {"fields": ["action", "reasoning"]}
      field_values       — {"field": "action", "allowed": ["create_task", "close_project"]}
      min_length         — {"min_length": 10}
      max_length         — {"max_length": 5000}
      response_contains  — {"terms": ["meeting", "schedule"]}
      tool_call_made     — {"tool_name": "create_task"}
    """

    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class JudgeCriteria:
    """What the LLM-as-judge should evaluate."""
    correctness: bool = True
    relevance: bool = True
    completeness: bool = True
    extra_instructions: str = ""


@dataclass
class EvalCase:
    """A single eval case definition."""
    id: str
    category: str
    description: str
    run: Callable[[AppContext], Awaitable[dict[str, Any]]]
    structural_checks: list[StructuralCheck] = field(default_factory=list)
    judge_criteria: JudgeCriteria = field(default_factory=JudgeCriteria)
    timeout_seconds: float = 120.0


@dataclass
class StructuralCheckResult:
    check: StructuralCheck
    passed: bool
    detail: str = ""


@dataclass
class JudgeResult:
    overall: float = 0.0
    correctness: float = 0.0
    relevance: float = 0.0
    completeness: float = 0.0
    reasoning: str = ""
    passed: bool = False


@dataclass
class EvalCaseResult:
    case_id: str
    category: str
    passed: bool = False
    structural_results: list[StructuralCheckResult] = field(default_factory=list)
    judge_result: JudgeResult | None = None
    llm_response: str = ""
    llm_latency_seconds: float = 0.0
    input_messages: list[dict[str, Any]] = field(default_factory=list)
    llm_tokens_used: int | None = None
    judge_latency_seconds: float | None = None
    error_message: str | None = None
