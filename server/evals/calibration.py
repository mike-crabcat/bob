"""Judge calibration (docs/evals.md → Judge calibration).

A judge is an instrument; this measures it. The labelled set is JSONL at
~/data/evals/calibration.jsonl (out of the repo — transcripts carry real
prompts). One entry:

  {"id": "...", "case_id": "...", "description": "...", "context": "...",
   "rubric": ["item", ...], "transcript": "...", "response": "...",
   "label": "pass" | "fail", "item_labels": [true, false, ...] | null,
   "labeller": "mike" | "claude-draft", "note": "why"}

``bob eval judge-calibrate`` re-judges every labelled entry with the
current judge prompt/model and reports overall agreement, Cohen's κ, the
confusion matrix, per-item agreement where item labels exist, and every
disagreement. ``bob eval calibration-draft`` pulls candidate trials from
the store (legacy rows included) into an unlabelled draft file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from server.evals.case import EvalCase


def default_path(settings: Any) -> Path:
    return Path(settings.data_dir) / "evals" / "calibration.jsonl"


def load(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        e = json.loads(line)
        if e.get("label") in ("pass", "fail"):
            out.append(e)
    return out


def kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    """Cohen's κ for two binary raters."""
    n = len(pairs)
    if n == 0:
        return None
    po = sum(a == b for a, b in pairs) / n
    pa = sum(a for a, _ in pairs) / n
    pb = sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe == 1 else (po - pe) / (1 - pe)


async def calibrate(ctx: Any, entries: list[dict[str, Any]], *,
                    judge_model: str | None = None, judge_effort: str | None = None,
                    show: bool = True,
                    progress: Callable[[str], None] | None = None) -> str:
    from server.evals.judge import judge

    say = progress or (lambda s: None)
    overall: list[tuple[bool, bool]] = []
    items: list[tuple[bool, bool]] = []
    disagreements: list[str] = []
    errors = 0
    for i, e in enumerate(entries, 1):
        case = EvalCase(id=e.get("case_id", "?"), category="calibration",
                        description=e.get("description", ""), run=_noop,
                        rubric=e["rubric"], context=e.get("context", ""))
        res = await judge(ctx, case, transcript=e["transcript"],
                          response=e.get("response", ""), judge_model=judge_model,
                          judge_effort=judge_effort)
        if res.error:
            errors += 1
            say(f"  [{i}/{len(entries)}] {e.get('id')}: judge error {res.error}")
            continue
        human = e["label"] == "pass"
        overall.append((human, res.passed))
        say(f"  [{i}/{len(entries)}] {e.get('id')}: human {e['label']} / "
            f"judge {'pass' if res.passed else 'fail'}")
        if e.get("item_labels"):
            for hl, v in zip(e["item_labels"], res.verdicts):
                items.append((bool(hl), v.passed))
        if human != res.passed:
            disagreements.append(
                f"- {e.get('id')} ({e.get('case_id')}): human {e['label']}, judge "
                f"{'pass' if res.passed else 'fail'}. Note: {e.get('note', '')}\n"
                + "\n".join(f"    [{'PASS' if v.passed else 'FAIL'}] {v.item[:90]} — {v.why}"
                            for v in res.verdicts))
    n = len(overall)
    if not n:
        return "no entries judged"
    agree = sum(a == b for a, b in overall)
    tp = sum(a and b for a, b in overall)
    tn = sum((not a) and (not b) for a, b in overall)
    fp = sum((not a) and b for a, b in overall)   # judge passed a human fail
    fn = sum(a and (not b) for a, b in overall)
    k = kappa(overall)
    from server.evals.judge import judge_label
    lines = [
        f"judge={judge_label(judge_model, judge_effort)}"
        f" · {n} entries ({errors} judge errors)",
        f"overall agreement {agree}/{n} = {agree / n:.0%} · Cohen's κ = "
        f"{'n/a' if k is None else f'{k:.2f}'}",
        f"confusion (human→judge): pass→pass {tp}, fail→fail {tn}, "
        f"fail→PASS {fp} (lenient), pass→FAIL {fn} (strict)",
    ]
    if items:
        ia = sum(a == b for a, b in items)
        ik = kappa(items)
        lines.append(f"item-level agreement {ia}/{len(items)} = {ia / len(items):.0%}"
                     f" · κ = {'n/a' if ik is None else f'{ik:.2f}'}")
    if show and disagreements:
        lines.append("\ndisagreements:")
        lines.extend(disagreements)
    return "\n".join(lines)


async def _noop(world: Any) -> None:
    return None


async def draft_entries(db: Any, out: Path, *, per_case: int = 2,
                        limit: int = 40) -> int:
    """Pick recent trials (mixed pass/fail per case where available) and
    write unlabelled entries with the CURRENT case rubric attached."""
    from server.evals.registry import get_all_cases
    from server.evals.store import EvalStore

    cases = {c.id: c for c in get_all_cases() if c.rubric}
    rows = await EvalStore(db).trials_for_calibration(list(cases), limit=2000)
    picked: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        bucket = picked.setdefault(r["case_id"], [])
        if len(bucket) >= per_case:
            continue
        if bucket and all(b["passed"] == r["passed"] for b in bucket) \
                and len(bucket) == per_case - 1:
            continue  # leave the last slot for the other outcome if it comes
        bucket.append(r)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w") as f:
        for cid, bucket in picked.items():
            case = cases[cid]
            for r in bucket:
                if n >= limit:
                    break
                f.write(json.dumps({
                    "id": f"{cid}:{r['id'][:8]}", "case_id": cid,
                    "description": case.description, "context": case.context,
                    "rubric": case.rubric,
                    "transcript": _transcript_of(r), "response": r["llm_response"],
                    "label": None, "item_labels": None, "labeller": None,
                    "note": "", "source_run": r["run_id"],
                    "old_verdict": "pass" if r["passed"] else "fail",
                }) + "\n")
                n += 1
    return n


def _transcript_of(row: dict[str, Any]) -> str:
    raw = row.get("input_messages_json") or ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, str):
        return data            # v2 rows store the rendered transcript
    lines = []
    for m in data if isinstance(data, list) else []:
        if not isinstance(m, dict):
            continue
        if m.get("type") == "function_call":
            lines.append(f"[Bob called tool] {m.get('name')}({str(m.get('arguments'))[:800]})")
        elif m.get("type") == "function_call_output":
            lines.append(f"[tool result] {str(m.get('output'))[:1500]}")
        elif m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                lines.append(f"[Bob called tool] {fn.get('name')}({str(fn.get('arguments'))[:800]})")
        else:
            c = m.get("content")
            c = c if isinstance(c, str) else json.dumps(c, default=str)
            if m.get("role") == "system" and len(c) > 8000:
                c = c[:4000] + "\n[… elided …]\n" + c[-4000:]
            lines.append(f"[{m.get('role')}] {c}")
    lines.append(f"[FINAL TEXT] {row.get('llm_response') or '(empty)'}")
    return "\n".join(lines)
