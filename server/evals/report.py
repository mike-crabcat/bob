"""Text reports for eval runs: summary, failure classes, run-vs-run compare."""

from __future__ import annotations

from typing import Any

from server.evals.stats import (
    Rate, case_stats, fisher_exact, newcombe_diff, rollup,
)


def run_header(run: dict[str, Any]) -> str:
    bits = [f"run {run['id'][:8]}", (run.get("started_at") or "")[:16],
            f"model={run.get('model') or '?'}"]
    if run.get("effort"):
        bits.append(f"effort={run['effort']}")
    if run.get("provider_pin"):
        bits.append(f"providers={run['provider_pin']}")
    bits.append(f"repeats={run.get('repeats') or 1}")
    if run.get("git_sha"):
        bits.append(f"git={run['git_sha']}{'+dirty' if run.get('git_dirty') else ''}")
    if run.get("persona_hash"):
        bits.append(f"persona={run['persona_hash']}")
    if run.get("judge_model"):
        bits.append(f"judge={run['judge_model']}")
    if run.get("total_cost_usd") is not None:
        bits.append(f"cost=${run['total_cost_usd']:.2f}")
    return " · ".join(bits)


def summary(run: dict[str, Any], trials: list[dict[str, Any]]) -> str:
    stats = case_stats(trials)
    roll = rollup(stats)
    reps = run.get("repeats") or 1
    lines = [run_header(run), ""]
    lines.append(f"{'CASE':<46} {'PASS RATE [95% CI]':<26} {'ALL':<4} FAILURES")
    for s in stats:
        fails = ", ".join(f"{k}×{v}" for k, v in sorted(s.failure_classes.items()))
        lines.append(f"{s.case_id[:46]:<46} {s.trials.fmt():<26} "
                     f"{'✓' if s.all_pass else '·':<4} {fails}")
    lines.append("")
    lines.append(f"{'CATEGORY':<24} {'CASES':>5}  {'TRIAL PASS RATE':<26} "
                 f"pass^{reps} (every repeat passed)")
    for cat, r in roll.items():
        name = "OVERALL" if cat == "*" else cat
        lines.append(f"{name:<24} {r['cases']:>5}  {r['trials'].fmt():<26} "
                     f"{r['pass_all'].fmt()}")
    classes: dict[str, int] = {}
    for t in trials:
        if not t["passed"]:
            fc = t.get("failure_class") or "unclassified"
            classes[fc] = classes.get(fc, 0) + 1
    if classes:
        lines.append("")
        lines.append("failures by class: " + ", ".join(
            f"{k} {v}" for k, v in sorted(classes.items(), key=lambda kv: -kv[1])))
    lat = [t["llm_latency_seconds"] for t in trials if t.get("llm_latency_seconds")]
    tok = [(t.get("prompt_tokens") or 0) + (t.get("completion_tokens") or 0)
           for t in trials]
    if lat:
        lat.sort()
        lines.append(f"latency per trial: median {lat[len(lat) // 2]:.0f}s, "
                     f"p90 {lat[int(len(lat) * 0.9)]:.0f}s · tokens per trial: "
                     f"median {sorted(tok)[len(tok) // 2]:,}")
    if reps < 5:
        lines.append(f"note: at {reps} repeat(s) no single case can show a "
                     "significant difference — read categories, not cases.")
    return "\n".join(lines)


def compare(run_a: dict[str, Any], trials_a: list[dict[str, Any]],
            run_b: dict[str, Any], trials_b: list[dict[str, Any]],
            *, alpha: float = 0.05) -> str:
    sa = {s.case_id: s for s in case_stats(trials_a)}
    sb = {s.case_id: s for s in case_stats(trials_b)}
    ra, rb = rollup(list(sa.values())), rollup(list(sb.values()))
    lines = ["A: " + run_header(run_a), "B: " + run_header(run_b), ""]
    warn = []
    for key, label in (("persona_hash", "persona/fixtures"), ("git_sha", "code"),
                       ("judge_model", "judge")):
        if run_a.get(key) != run_b.get(key):
            warn.append(f"{label} differs ({run_a.get(key)} vs {run_b.get(key)})")
    if warn:
        lines.append("⚠ not a clean comparison: " + "; ".join(warn))
        lines.append("")
    lines.append(f"{'CATEGORY':<24} {'A':<22} {'B':<22} {'B−A [95% CI]':<24}")
    for cat in sorted(set(ra) | set(rb), key=lambda c: (c == "*", c)):
        a = ra.get(cat, {}).get("trials", Rate(0, 0))
        b = rb.get(cat, {}).get("trials", Rate(0, 0))
        d, lo, hi = newcombe_diff(a.k, a.n, b.k, b.n)
        sig = " *" if (lo > 0 or hi < 0) and a.n and b.n else ""
        lines.append(f"{'OVERALL' if cat == '*' else cat:<24} "
                     f"{a.rate:4.0%} ({a.k}/{a.n}){'':<8} "
                     f"{b.rate:4.0%} ({b.k}/{b.n}){'':<8} "
                     f"{d:+.0%} [{lo:+.0%}, {hi:+.0%}]{sig}")
    lines.append("  (* = interval excludes 0)")
    flips = []
    for cid in sorted(set(sa) & set(sb)):
        a, b = sa[cid].trials, sb[cid].trials
        if a.rate == b.rate:
            continue
        p = fisher_exact(a.k, a.n, b.k, b.n)
        flips.append((p, cid, a, b))
    flips.sort()
    lines.append("")
    lines.append("per-case changes (Fisher exact, two-sided):")
    if not flips:
        lines.append("  none")
    for p, cid, a, b in flips:
        tag = "SIGNIFICANT" if p < alpha else ("consistent flip" if
              {a.rate, b.rate} == {0.0, 1.0} else "")
        lines.append(f"  {cid[:44]:<44} {a.k}/{a.n} → {b.k}/{b.n}  p={p:.3f} {tag}")
    only = sorted(set(sa) ^ set(sb))
    if only:
        lines.append(f"cases in only one run: {', '.join(only)}")
    return "\n".join(lines)
