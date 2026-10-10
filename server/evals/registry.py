"""Eval case registry and the @eval_case decorator."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from server.evals.case import Check, EvalCase

logger = logging.getLogger(__name__)

_REGISTERED: list[EvalCase] = []
_DISCOVERED = False


def eval_case(
    *,
    id: str,
    category: str,
    description: str,
    checks: list[Check] | None = None,
    rubric: list[str] | None = None,
    context: str = "",
    tier: str = "discipline",
    timeout_seconds: float = 300.0,
    expects_turn: bool = True,
) -> Any:
    """Register ``async def case(world)`` as an eval case."""
    def decorator(func: Any) -> Any:
        if any(c.id == id for c in _REGISTERED):
            raise ValueError(f"duplicate eval case id {id!r}")
        _REGISTERED.append(EvalCase(
            id=id, category=category, description=description, run=func,
            checks=checks or [], rubric=rubric or [], context=context,
            tier=tier, timeout_seconds=timeout_seconds,
            expects_turn=expects_turn))
        return func
    return decorator


def _discover() -> None:
    global _DISCOVERED
    _DISCOVERED = True
    for sub in ("cases", "scenarios"):
        d = Path(__file__).parent / sub
        if not d.is_dir():
            continue
        for py_file in sorted(d.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            importlib.import_module(f"server.evals.{sub}.{py_file.stem}")


def get_all_cases() -> list[EvalCase]:
    if not _DISCOVERED:
        _discover()
    return list(_REGISTERED)


def select(*, category: str | None = None, case_ids: list[str] | None = None,
           tier: str | None = None) -> list[EvalCase]:
    cases = get_all_cases()
    if case_ids:
        wanted = set(case_ids)
        unknown = wanted - {c.id for c in cases}
        if unknown:
            raise KeyError(f"unknown case id(s): {sorted(unknown)}")
        return [c for c in cases if c.id in wanted]
    if category:
        cases = [c for c in cases if c.category == category]
    if tier:
        cases = [c for c in cases if c.tier == tier]
    return cases
