"""Structural and LLM-as-judge evaluators."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from server.evals.case import (
    EvalCase,
    JudgeCriteria,
    JudgeResult,
    StructuralCheck,
    StructuralCheckResult,
)
from server.context import AppContext

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

    def _check_any_tool_call(self, response: str, check: StructuralCheck, ctx: dict) -> StructuralCheckResult:
        """OR-semantics twin: passes when AT LEAST ONE of the named tools
        fired (recall-OR-find shapes — the memory mandate names both)."""
        tool_calls = ctx.get("tool_calls", [])
        names = check.params.get("tool_names", [])
        if not names:
            return StructuralCheckResult(
                check=check, passed=False,
                detail="any_tool_call requires tool_names",
            )
        if not any(tc.get("name") in names for tc in tool_calls):
            return StructuralCheckResult(
                check=check, passed=False,
                detail=f"none of {names} called (all calls: "
                       f"{[tc.get('name', '?') for tc in tool_calls]})",
            )
        return StructuralCheckResult(check=check, passed=True)

    def _check_no_tool_call(self, response: str, check: StructuralCheck, ctx: dict) -> StructuralCheckResult:
        """Negative twin of tool_call_made — the guard-rail check for
        unintended-outcome cases (permission theater must not act; banter
        must not recall-spam). Passes when NONE of the named tools fired."""
        tool_calls = ctx.get("tool_calls", [])
        names = check.params.get("tool_names", [])
        if not names:
            return StructuralCheckResult(
                check=check, passed=False,
                detail="no_tool_call requires tool_names",
            )
        fired = sorted({tc.get("name", "?") for tc in tool_calls
                        if tc.get("name") in names})
        if fired:
            return StructuralCheckResult(
                check=check, passed=False,
                detail=f"forbidden call(s): {fired} (all calls: "
                       f"{[tc.get('name', '?') for tc in tool_calls]})",
            )
        return StructuralCheckResult(check=check, passed=True)

    def _check_tool_call_args(self, response: str, check: StructuralCheck, ctx: dict) -> StructuralCheckResult:
        """Routing hinge: not WHETHER a tool fired but with WHAT. Passes
        when at least one call to tool_name has every arg substring in its
        arguments string (e.g. create_subagent with agent_type='claude').
        Substrings, not JSON paths — arguments arrive as a string and the
        exact quoting varies by provider."""
        tool_calls = ctx.get("tool_calls", [])
        target = check.params.get("tool_name", "")
        need = check.params.get("arg_contains", [])
        candidates = [tc for tc in tool_calls if tc.get("name") == target]
        if not candidates:
            names = [tc.get("name", "?") for tc in tool_calls]
            return StructuralCheckResult(
                check=check, passed=False,
                detail=f"{target} not called (calls: {names})",
            )
        for tc in candidates:
            args = str(tc.get("arguments", ""))
            if all(str(sub) in args for sub in need):
                return StructuralCheckResult(check=check, passed=True)
        return StructuralCheckResult(
            check=check, passed=False,
            detail=f"{target} called but no call's arguments contain all of "
                   f"{need} (args seen: {[str(tc.get('arguments', ''))[:120] for tc in candidates]})",
        )

    def _check_response_not_contains(self, response: str, check: StructuralCheck, ctx: dict) -> StructuralCheckResult:
        """Negative twin of response_contains — permission-theater phrases
        ("shall I", "do you want me to") and asserted stats the case must
        not produce. Case-insensitive, like its twin."""
        terms = check.params.get("terms", [])
        lower = response.lower()
        found = [t for t in terms if t.lower() in lower]
        if found:
            return StructuralCheckResult(
                check=check, passed=False,
                detail=f"Forbidden term(s) in response: {found}",
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
        judge_model: str | None = None,
    ) -> JudgeResult:
        from server.services.llm_dispatch import LLMDispatchService

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
                # Responses-API items (what chat_with_tools actually appends
                # to the message list — without this branch the judge never
                # saw tool calls and scored completeness 0 for "did not call
                # the tool" on calls that fired; Phase 0 fix 2026-09-19).
                if msg.get("type") == "function_call":
                    formatted.append(
                        f"[assistant called tool]: {msg.get('name', '?')}"
                        f"({str(msg.get('arguments', ''))[:400]})")
                    continue
                if msg.get("type") == "function_call_output":
                    formatted.append(
                        f"[tool result]: {str(msg.get('output', ''))[:600]}")
                    continue
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls")

                if tool_calls:
                    call_descs = []
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        call_descs.append(f"{fn.get('name', '?')}({fn.get('arguments', '')})")
                    line = f"[{role} called tools]: " + ", ".join(call_descs)
                elif isinstance(content, str) and len(content) > 2000:
                    line = f"[{role}]: {content[:2000]}... [truncated]"
                else:
                    line = f"[{role}]: {content}"

                formatted.append(line)
            input_section = f"\nINPUT MESSAGES:\n" + "\n".join(formatted) + "\n"

        prompt = (
            f"You are an evaluation judge. Score the response on each dimension from 0.0 to 1.0.\n\n"
            f"EVAL CASE: {case.description}\n"
            f"{input_section}\n"
            f"RESPONSE TO EVALUATE:\n{response}\n\n"
            f"EVIDENCE RULE: lines marked [assistant called tool] in INPUT MESSAGES "
            f"are actions the assistant actually took (they are recorded tool "
            f"calls, not claims) — count them as demonstrated behavior even if "
            f"the response text doesn't restate them.\n"
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
                model=judge_model or "gpt-5.4-nano",
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
