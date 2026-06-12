"""Eval case registry and @eval_case decorator."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from bob_server.evals.case import EvalCase, JudgeCriteria, StructuralCheck

logger = logging.getLogger(__name__)

_REGISTERED: list[EvalCase] = []


def eval_case(
    *,
    id: str,
    category: str,
    description: str,
    structural_checks: list[StructuralCheck] | None = None,
    judge_criteria: JudgeCriteria | None = None,
    timeout_seconds: float = 120.0,
) -> Any:
    """Decorator that registers an eval case.

    The decorated async function receives (ctx) and must return
    a dict with at minimum {"response": str}.
    """
    def decorator(func: Any) -> Any:
        case = EvalCase(
            id=id,
            category=category,
            description=description,
            run=func,
            structural_checks=structural_checks or [],
            judge_criteria=judge_criteria or JudgeCriteria(),
            timeout_seconds=timeout_seconds,
        )
        _REGISTERED.append(case)
        return func
    return decorator


def _discover() -> None:
    """Import all Python files in the cases/ directory."""
    cases_dir = Path(__file__).parent / "cases"
    if not cases_dir.is_dir():
        return
    for py_file in sorted(cases_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"bob_server.evals.cases.{py_file.stem}"
        try:
            importlib.import_module(module_name)
        except Exception:
            logger.warning("Failed to load eval cases from %s", py_file, exc_info=True)


def get_all_cases() -> list[EvalCase]:
    if not _REGISTERED:
        _discover()
    return list(_REGISTERED)


def get_cases_by_category(category: str) -> list[EvalCase]:
    return [c for c in get_all_cases() if c.category == category]


def get_case_by_id(case_id: str) -> EvalCase | None:
    return next((c for c in get_all_cases() if c.id == case_id), None)
