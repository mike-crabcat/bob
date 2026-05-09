"""Structural and LLM-as-judge evaluators."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from cyborg_server.evals.case import (
    EvalCase,
    JudgeCriteria,
    JudgeResult,
    StructuralCheck,
    StructuralCheckResult,
)
from cyborg_server.context import AppContext

logger = logging.getLogger(__name__)


class StructuralJudge:
    """Validates LLM responses against structural expectations."""

    def check(
        self,
        response: str,
        check_def: StructuralCheck,
        context: dict[str, Any] | None = None,
    ) -> StructuralCheckResult:
        handler = getattr(self, f"_check_{check_def.kind}", None)
        if handler is None:
            return StructuralCheckResult(
                check=check_def, passed=False,
                detail=f"Unknown check kind: {check_def.kind}",
            )
        return handler(response, check_def, context or {})

    def check_all(
        self,
        response: str,
        checks: list[StructuralCheck],
        context: dict[str, Any] | None = None,
    ) -> list[StructuralCheckResult]:
        return [self.check(response, c, context) for c in checks]

    def _check_json_valid(self, response: str, check: StructuralCheck, ctx: dict) -> StructuralCheckResult:
        try:
            json.loads(response)
            return StructuralCheckResult(check=check, passed=True)
        except json.JSONDecodeError as e:
            return StructuralCheckResult(check=check, passed=False, detail=str(e))

    def _check_min_length(self, response: str, check: StructuralCheck, ctx: dict) -> StructuralCheckResult:
        min_len = check.params.get("min_length", 0)
        passed = len(response.strip()) >= min_len
        return StructuralCheckResult(
            check=check, passed=passed,
            detail=f"{len(response.strip())} chars (min {min_len})" if not passed else "",
        )

    def _check_max_length(self, response: str, check: StructuralCheck, ctx: dict) -> StructuralCheckResult:
        max_len = check.params.get("max_length", float("inf"))
        passed = len(response.strip()) <= max_len
        return StructuralCheckResult(
            check=check, passed=passed,
            detail=f"{len(response.strip())} chars (max {max_len})" if not passed else "",
        )

    def _check_field_present(self, response: str, check: StructuralCheck, ctx: dict) -> StructuralCheckResult:
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            return StructuralCheckResult(check=check, passed=False, detail="Not valid JSON")
        missing = [f for f in check.params.get("fields", []) if f not in data or not data[f]]
        if missing:
            return StructuralCheckResult(check=check, passed=False, detail=f"Missing: {missing}")
        return StructuralCheckResult(check=check, passed=True)

    def _check_field_values(self, response: str, check: StructuralCheck, ctx: dict) -> StructuralCheckResult:
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            return StructuralCheckResult(check=check, passed=False, detail="Not valid JSON")
        field_name = check.params.get("field", "")
        allowed = check.params.get("allowed", [])
        value = data.get(field_name)
        if value not in allowed:
            return StructuralCheckResult(
                check=check, passed=False,
                detail=f"{field_name}={value!r}, allowed={allowed}",
            )
        return StructuralCheckResult(check=check, passed=True)

    def _check_json_schema(self, response: str, check: StructuralCheck, ctx: dict) -> StructuralCheckResult:
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            return StructuralCheckResult(check=check, passed=False, detail="Not valid JSON")
        errors: list[str] = []
        required = check.params.get("required_fields", [])
        for f in required:
            if f not in data:
                errors.append(f"missing required field: {f}")
        array_field = check.params.get("array_field")
        if array_field and array_field in data:
            arr = data[array_field]
            if not isinstance(arr, list):
                errors.append(f"{array_field} is not an array")
            else:
                min_items = check.params.get("min_items", 0)
                max_items = check.params.get("max_items", float("inf"))
                if len(arr) < min_items:
                    errors.append(f"{array_field} has {len(arr)} items (min {min_items})")
                if len(arr) > max_items:
                    errors.append(f"{array_field} has {len(arr)} items (max {max_items})")
                item_required = check.params.get("item_required_fields", [])
                for i, item in enumerate(arr):
                    if isinstance(item, dict):
                        for f in item_required:
                            if f not in item:
                                errors.append(f"{array_field}[{i}] missing '{f}'")
        if errors:
            return StructuralCheckResult(check=check, passed=False, detail="; ".join(errors))
        return StructuralCheckResult(check=check, passed=True)

    def _check_response_contains(self, response: str, check: StructuralCheck, ctx: dict) -> StructuralCheckResult:
        terms = check.params.get("terms", [])
        lower = response.lower()
        found = [t for t in terms if t.lower() in lower]
        if not found:
            return StructuralCheckResult(
                check=check, passed=False,
                detail=f"None of {terms} found in response",
            )
        return StructuralCheckResult(check=check, passed=True)

    def _check_tool_call_made(self, response: str, check: StructuralCheck, ctx: dict) -> StructuralCheckResult:
        tool_calls = ctx.get("tool_calls", [])
        target = check.params.get("tool_name", "")
        found = any(tc.get("name") == target for tc in tool_calls)
        if not found:
            names = [tc.get("name", "?") for tc in tool_calls]
            return StructuralCheckResult(
                check=check, passed=False,
                detail=f"{target} not called (calls: {names})",
            )
        return StructuralCheckResult(check=check, passed=True)


class LLMJudge:
    """Uses an LLM call to evaluate response quality."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def judge(
        self,
        case: EvalCase,
        response: str,
        threshold: float = 0.7,
        input_messages: list[dict[str, Any]] | None = None,
    ) -> JudgeResult:
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        dimensions = []
        if case.judge_criteria.correctness:
            dimensions.append("- correctness: Is the response factually correct given the input?")
        if case.judge_criteria.relevance:
            dimensions.append("- relevance: Does the response address the input?")
        if case.judge_criteria.completeness:
            dimensions.append("- completeness: Does the response cover all expected aspects?")

        extra = ""
        if case.judge_criteria.extra_instructions:
            extra = f"\nADDITIONAL GUIDANCE:\n{case.judge_criteria.extra_instructions}"

        input_section = ""
        if input_messages:
            formatted = []
            for msg in input_messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 2000:
                    content = content[:2000] + "... [truncated]"
                formatted.append(f"[{role}]: {content}")
            input_section = f"\nINPUT MESSAGES:\n" + "\n".join(formatted) + "\n"

        prompt = (
            f"You are an evaluation judge. Score the response on each dimension from 0.0 to 1.0.\n\n"
            f"EVAL CASE: {case.description}\n"
            f"{input_section}\n"
            f"RESPONSE TO EVALUATE:\n{response}\n\n"
            f"DIMENSIONS:\n" + "\n".join(dimensions) + extra +
            "\n\nRespond with valid JSON only:\n"
            '{"correctness": 0.0, "relevance": 0.0, "completeness": 0.0, '
            '"overall": 0.0, "reasoning": "brief explanation"}'
        )

        dispatch = LLMDispatchService(self.ctx)
        t0 = time.monotonic()
        try:
            judge_response = await dispatch.chat(
                [{"role": "user", "content": prompt}],
                call_category="eval_judge",
                temperature=0.3,
            )
            data = json.loads(judge_response)
            overall = float(data.get("overall", 0))
            return JudgeResult(
                overall=overall,
                correctness=float(data.get("correctness", 0)),
                relevance=float(data.get("relevance", 0)),
                completeness=float(data.get("completeness", 0)),
                reasoning=data.get("reasoning", ""),
                passed=overall >= threshold,
            )
        except Exception as e:
            logger.warning("LLM judge failed: %s", e)
            return JudgeResult(reasoning=f"Judge call failed: {e}")
