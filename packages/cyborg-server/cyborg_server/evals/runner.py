"""Eval runner: orchestrates execution and records results."""

from __future__ import annotations

import json
import logging
import time
from uuid import uuid4

from cyborg_server.evals.case import EvalCase, EvalCaseResult
from cyborg_server.evals.judge import LLMJudge, StructuralJudge
from cyborg_server.evals.registry import get_all_cases, get_cases_by_category, get_case_by_id

logger = logging.getLogger(__name__)


class EvalRunner:
    """Orchestrates running eval cases and recording results."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.structural = StructuralJudge()
        self.llm_judge = LLMJudge(ctx)

    async def run_all(
        self,
        *,
        category: str | None = None,
        case_id: str | None = None,
        judge_threshold: float = 0.7,
        skip_judge: bool = False,
    ) -> list[EvalCaseResult]:
        if case_id:
            cases = [c for c in get_all_cases() if c.id == case_id]
        elif category:
            cases = get_cases_by_category(category)
        else:
            cases = get_all_cases()

        if not cases:
            logger.info("No eval cases found")
            return []

        run_id = str(uuid4())
        await self._record_run_start(run_id, category)

        results: list[EvalCaseResult] = []
        for case in cases:
            print(f"  Running: {case.id} ({case.category})...", end=" ", flush=True)
            result = await self._run_case(case, judge_threshold, skip_judge)
            results.append(result)
            status = "PASS" if result.passed else "FAIL"
            print(status)
            await self._record_case_result(run_id, result)

        passed = sum(1 for r in results if r.passed)
        await self._record_run_complete(run_id, results)
        return results

    async def _run_case(
        self,
        case: EvalCase,
        judge_threshold: float,
        skip_judge: bool,
    ) -> EvalCaseResult:
        t0 = time.monotonic()
        try:
            output = await case.run(self.ctx)
            response = output.get("response", "")
            context = output.get("context", {})
            elapsed = time.monotonic() - t0

            # Structural checks
            struct_results = self.structural.check_all(response, case.structural_checks, context)
            all_structural_pass = all(r.passed for r in struct_results)

            # LLM judge
            judge_result = None
            if not skip_judge and case.judge_criteria.extra_instructions:
                judge_result = await self.llm_judge.judge(case, response, judge_threshold)

            passed = all_structural_pass and (judge_result is None or judge_result.passed)

            return EvalCaseResult(
                case_id=case.id,
                category=case.category,
                passed=passed,
                structural_results=struct_results,
                judge_result=judge_result,
                llm_response=response,
                llm_latency_seconds=elapsed,
            )
        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.error("Eval case %s failed: %s", case.id, e, exc_info=True)
            return EvalCaseResult(
                case_id=case.id,
                category=case.category,
                passed=False,
                llm_latency_seconds=elapsed,
                error_message=str(e),
            )

    async def _record_run_start(self, run_id: str, category: str | None) -> None:
        try:
            await self.ctx.db.execute(
                "INSERT INTO eval_runs (id, category, status) VALUES (?, ?, 'running')",
                (run_id, category),
            )
        except Exception:
            logger.warning("Failed to record eval run start", exc_info=True)

    async def _record_run_complete(self, run_id: str, results: list[EvalCaseResult]) -> None:
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        rate = passed / total if total else 0
        try:
            await self.ctx.db.execute(
                "UPDATE eval_runs SET completed_at = datetime('now'), "
                "total_cases = ?, passed_cases = ?, failed_cases = ?, "
                "overall_pass_rate = ?, status = 'completed' WHERE id = ?",
                (total, passed, total - passed, rate, run_id),
            )
        except Exception:
            logger.warning("Failed to record eval run complete", exc_info=True)

    async def _record_case_result(self, run_id: str, result: EvalCaseResult) -> None:
        try:
            struct_json = json.dumps([
                {"kind": r.check.kind, "passed": r.passed, "detail": r.detail}
                for r in result.structural_results
            ])
            await self.ctx.db.execute(
                """INSERT INTO eval_case_results
                   (id, run_id, case_id, category, passed, llm_response,
                    llm_latency_seconds, judge_score, judge_reasoning,
                    structural_results_json, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid4()), run_id, result.case_id, result.category,
                    1 if result.passed else 0,
                    result.llm_response[:50000],
                    result.llm_latency_seconds,
                    result.judge_result.overall if result.judge_result else None,
                    result.judge_result.reasoning if result.judge_result else None,
                    struct_json,
                    result.error_message,
                ),
            )
        except Exception:
            logger.warning("Failed to record eval case result", exc_info=True)


from typing import Any
