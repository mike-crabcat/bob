"""Eval statistics — pure functions, no I/O (docs/evals.md → Statistics).

One trial per case was noise: two same-model gpt-6-luna runs agreed on
the total (40 vs 41 of 53) while ~10 individual cases flipped. Every
number the suite reports now carries its uncertainty:

- per case: k passes of n trials → pass rate + Wilson 95% interval, and
  pass^n (did EVERY repeat pass — reliability, the production property);
- per category / overall: pooled trials → rate + Wilson interval. Pooling
  ignores within-case correlation, so these intervals are optimistic —
  good enough to rank, not to publish;
- run vs run: Newcombe hybrid-score interval on the difference of two
  pooled rates, and a two-sided Fisher exact test per case.

With the default 3 repeats no single case can reach p < 0.05 (3/3 vs
0/3 is p = 0.10): per-case flips at n=3 are indicative only; use
--repeats 5 or more when a per-case verdict matters (5/5 vs 0/5 is
p ≈ 0.008).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials. (0, 1) when n=0."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def newcombe_diff(k1: int, n1: int, k2: int, n2: int,
                  z: float = Z95) -> tuple[float, float, float]:
    """Difference p2 - p1 with Newcombe's hybrid score 95% interval
    (method 10) — well-behaved at 0/1 rates where Wald intervals collapse.
    Returns (delta, lo, hi)."""
    p1 = k1 / n1 if n1 else 0.0
    p2 = k2 / n2 if n2 else 0.0
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    d = p2 - p1
    lo = d - math.sqrt((p2 - l2) ** 2 + (u1 - p1) ** 2)
    hi = d + math.sqrt((u2 - p2) ** 2 + (p1 - l1) ** 2)
    return d, max(-1.0, lo), min(1.0, hi)


def fisher_exact(k1: int, n1: int, k2: int, n2: int) -> float:
    """Two-sided Fisher exact p for the 2x2 table [[k1, n1-k1], [k2, n2-k2]]."""
    if n1 <= 0 or n2 <= 0:
        return 1.0
    total_k = k1 + k2
    total_n = n1 + n2

    def hyper(a: int) -> float:
        return (math.comb(n1, a) * math.comb(n2, total_k - a)
                / math.comb(total_n, total_k))

    lo = max(0, total_k - n2)
    hi = min(n1, total_k)
    observed = hyper(k1)
    p = sum(h for a in range(lo, hi + 1)
            if (h := hyper(a)) <= observed * (1 + 1e-9))
    return min(1.0, p)


@dataclass
class Rate:
    """k of n with its interval."""
    k: int
    n: int

    @property
    def rate(self) -> float:
        return self.k / self.n if self.n else 0.0

    @property
    def ci(self) -> tuple[float, float]:
        return wilson(self.k, self.n)

    def fmt(self) -> str:
        lo, hi = self.ci
        return f"{self.rate:4.0%} [{lo:.0%}–{hi:.0%}] ({self.k}/{self.n})"


@dataclass
class CaseStats:
    case_id: str
    category: str
    trials: Rate
    all_pass: bool        # pass^n: every repeat passed
    failure_classes: dict[str, int]


def case_stats(rows: Iterable[dict]) -> list[CaseStats]:
    """Group trial rows (case_id, category, passed, failure_class) per case."""
    by_case: dict[str, dict] = {}
    for r in rows:
        c = by_case.setdefault(r["case_id"], {
            "category": r["category"], "k": 0, "n": 0, "fails": {}})
        c["n"] += 1
        if r["passed"]:
            c["k"] += 1
        elif r.get("failure_class"):
            fc = r["failure_class"]
            c["fails"][fc] = c["fails"].get(fc, 0) + 1
    return [CaseStats(case_id=cid, category=c["category"],
                      trials=Rate(c["k"], c["n"]),
                      all_pass=c["k"] == c["n"] and c["n"] > 0,
                      failure_classes=c["fails"])
            for cid, c in sorted(by_case.items())]


def rollup(stats: list[CaseStats]) -> dict[str, dict]:
    """Per-category (plus '*' overall) pooled rate and pass^n share."""
    out: dict[str, dict] = {}
    for key in sorted({s.category for s in stats}) + ["*"]:
        group = [s for s in stats if key == "*" or s.category == key]
        k = sum(s.trials.k for s in group)
        n = sum(s.trials.n for s in group)
        out[key] = {"cases": len(group), "trials": Rate(k, n),
                    "pass_all": Rate(sum(1 for s in group if s.all_pass),
                                     len(group))}
    return out
