"""Eval runner v2 (docs/evals.md → Running).

One run = one template world + N trials per case (repeats), executed with
bounded concurrency, each trial in its own throwaway world. Results land
in the eval store (~/data/evals.db) with everything needed to compare
runs: model, provider pin, effort, judge model, git sha/dirty, persona
hash, per-trial tokens/cost/latency and a failure class.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from server.context import AppContext
from server.database import Database
from server.evals import judge as judging
from server.evals import transcript as tx
from server.evals.case import EvalCase, Trial
from server.evals.store import EvalStore
from server.evals.world import SCHEMA_DIR, Template, World

logger = logging.getLogger(__name__)

FAILURE_CLASSES = ("harness_error", "provider_error", "timeout", "tool_format",
                   "empty_response", "behavioural")

_PROVIDER_ERR = re.compile(
    r"rate.?limit|429|5\d\d\b|overloaded|timed? ?out|timeout|connection|"
    r"APIConnection|APIStatus|InternalServer|ServiceUnavailable|"
    r"insufficient_quota|credit|provider|upstream|ReadError|RemoteProtocol",
    re.I)
_LEAKED_TOOL_TEXT = re.compile(
    r"<tool_call>|</tool_call>|\[tools used:|<function=|\"name\"\s*:\s*\"\w+\"\s*,"
    r"\s*\"arguments\"", re.I)


class HarnessError(RuntimeError):
    """The world/case broke, not the model."""


class RunAborted(RuntimeError):
    """Something run-wide is broken (judge out of credit) — stop rather
    than record a run of harness errors."""


_FATAL_JUDGE = re.compile(r"no credits|insufficient_quota|credit_balance|"
                          r"billing|invalid_api_key|exceeded your current quota", re.I)
_FATAL_AFTER = 3


@dataclass
class RunConfig:
    model: str | None = None
    effort: str | None = None
    judge_model: str | None = None
    judge_effort: str | None = None
    repeats: int = 3
    concurrency: int = 3
    category: str | None = None
    case_ids: list[str] | None = None
    tier: str | None = None
    skip_judge: bool = False
    notes: str = ""
    progress: Callable[[str], None] | None = None


@dataclass
class RunResult:
    run_id: str
    trials: list[Trial] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def eval_store_path(settings: Any) -> Path:
    env = os.getenv("BOB_EVAL_DB")
    return Path(env).expanduser() if env else Path(settings.data_dir) / "evals.db"


async def open_store(settings: Any) -> tuple[Database, AppContext]:
    """The results store + a context for judge calls (their llm_call_log
    rows land in the store, never in a world or the live DB)."""
    path = eval_store_path(settings)
    live = Path(settings.db_path).expanduser().resolve() if settings.db_path else None
    if live is not None and path.expanduser().resolve() == live:
        raise RuntimeError("eval store must not be the live bob.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_path=path, schema_dir=SCHEMA_DIR, pool_size=2)
    await db.connect()
    await db.apply_migrations()
    return db, AppContext(db=db, settings=settings)


def git_state() -> tuple[str | None, bool | None]:
    root = Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root,
                             capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
            capture_output=True, text=True, timeout=10).stdout.strip())
        return sha or None, dirty
    except Exception:
        return None, None


def provider_pin(settings: Any) -> str | None:
    pins = getattr(getattr(settings, "openrouter", None), "providers", None)
    if pins:
        return ",".join(pins) if isinstance(pins, (list, tuple)) else str(pins)
    return os.getenv("BOB_OPENROUTER_PROVIDERS") or None


class EvalRunner:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def run(self, cases: list[EvalCase], cfg: RunConfig) -> RunResult:
        from server.evals import intercept
        intercept.install()
        say = cfg.progress or (lambda s: None)

        store_db, store_ctx = await open_store(self.settings)
        store = EvalStore(store_db)
        template = await Template.build(self.settings)
        sha, dirty = git_state()
        meta = {
            "model": cfg.model or self.settings.openai.default_model,
            "provider_pin": provider_pin(self.settings),
            "effort": cfg.effort,
            "judge_model": (cfg.judge_model or judging.DEFAULT_JUDGE_MODEL)
            + (f"@{cfg.judge_effort}" if cfg.judge_effort else ""),
            "git_sha": sha,
            "git_dirty": None if dirty is None else int(dirty),
            "persona_hash": template.persona_hash, "repeats": cfg.repeats,
            "tier": cfg.tier,
            "case_filter": ",".join(cfg.case_ids) if cfg.case_ids else None,
            "notes": cfg.notes or None,
        }
        run_id = await store.start_run(category=cfg.category, meta=meta)
        say(f"run {run_id[:8]} · {len(cases)} case(s) × {cfg.repeats} · "
            f"model {meta['model']} · persona {template.persona_hash}"
            f"{' · DIRTY tree' if dirty else ''}")

        sem = asyncio.Semaphore(max(1, cfg.concurrency))
        trials: list[Trial] = []
        work = [(c, r) for r in range(cfg.repeats) for c in cases]
        fatal: list[str] = []

        async def one(case: EvalCase, rep: int) -> None:
            async with sem:
                if len(fatal) >= _FATAL_AFTER:
                    return
                trial = await self._trial(case, rep, template, cfg, store_ctx)
                if trial.error and _FATAL_JUDGE.search(trial.error):
                    fatal.append(trial.error)
                    if len(fatal) >= _FATAL_AFTER:
                        say(f"ABORTING: judge failing run-wide — {trial.error[:200]}")
                    return            # not recorded: not a fact about the model
                trials.append(trial)
                await store.record_trial(run_id, _trial_row(trial))
                mark = "PASS" if trial.passed else f"FAIL[{trial.failure_class}]"
                say(f"  {case.id} #{rep + 1}: {mark} ({trial.latency:.0f}s)")

        try:
            await asyncio.gather(*(one(c, r) for c, r in work))
            if len(fatal) >= _FATAL_AFTER:
                raise RunAborted(fatal[-1][:500])
            tokens = sum(t.prompt_tokens + t.completion_tokens for t in trials)
            costs = [t.cost_usd for t in trials if t.cost_usd is not None]
            await store.complete_run(
                run_id, total=len(trials), passed=sum(t.passed for t in trials),
                tokens=tokens, cost=round(sum(costs), 4) if costs else None)
        except BaseException:
            await store.complete_run(
                run_id, total=len(trials), passed=sum(t.passed for t in trials),
                tokens=None, cost=None, status="aborted")
            raise
        finally:
            template.cleanup()
            await store_db.close()
        return RunResult(run_id=run_id, trials=trials, meta=meta)

    async def _trial(self, case: EvalCase, rep: int, template: Template,
                     cfg: RunConfig, store_ctx: AppContext) -> Trial:
        trial = Trial(case_id=case.id, category=case.category, repeat_index=rep)
        t0 = time.monotonic()
        world: World | None = None
        try:
            world = await World.open(template, pin_model=cfg.model,
                                     effort=cfg.effort)
            with world.active():
                out = await asyncio.wait_for(case.run(world),
                                             timeout=case.timeout_seconds)
                await world.settle(timeout=60)
            trial.latency = time.monotonic() - t0
            override = out.get("response") if isinstance(out, dict) else None
            trial.response = override if override is not None else world.reply()
            trial.tool_calls = world.tool_calls()
            trial.deliveries = list(world.deliveries)
            trial.flags = dict(world.flags)
            trial.transcript = tx.render(world)
            await _usage(world, trial)
            if case.expects_turn and not world.main_turns:
                raise HarnessError(
                    "no Bob turn ran — the stimulus was stored but never "
                    "dispatched (a pass here would be the harness lying)")
            delivered = _delivered_by_label(world)
            trial.checks = judging.run_checks(
                case.checks, response=trial.response,
                tool_calls=trial.tool_calls, flags=trial.flags,
                delivered=delivered,
                any_delivery=any(d["kind"].startswith("whatsapp_send")
                                 for d in world.deliveries))
            checks_ok = all(c.passed for c in trial.checks)
            if checks_ok and case.rubric and not cfg.skip_judge:
                trial.judge = await judging.judge(
                    store_ctx, case, transcript=trial.transcript,
                    response=trial.response, judge_model=cfg.judge_model,
                    judge_effort=cfg.judge_effort)
            trial.passed = checks_ok and (trial.judge is None or trial.judge.passed)
            if trial.judge is not None and trial.judge.error:
                trial.failure_class = "harness_error"
                trial.error = trial.judge.reasoning
            elif not trial.passed:
                trial.failure_class = _classify(world, trial)
        except Exception as exc:
            trial.latency = time.monotonic() - t0
            trial.error = f"{type(exc).__name__}: {exc}"[:2000]
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                trial.error = (f"case exceeded {case.timeout_seconds:.0f}s "
                               f"(turns so far: {len(world.main_turns) if world else 0})")
            trial.failure_class = "timeout" if isinstance(
                exc, (asyncio.TimeoutError, TimeoutError)) else ("provider_error"
                                   if _PROVIDER_ERR.search(trial.error)
                                   and not isinstance(exc, (HarnessError, AssertionError,
                                                            KeyError, AttributeError,
                                                            TypeError))
                                   else "harness_error")
            if world is not None:
                trial.transcript = tx.render(world)
                trial.tool_calls = world.tool_calls()
                await _usage(world, trial)
            logger.warning("trial %s#%d raised: %s", case.id, rep, trial.error,
                           exc_info=not isinstance(exc, asyncio.TimeoutError))
        finally:
            if world is not None:
                await world.close()
        return trial


def _delivered_by_label(w: World) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in w.people.values():
        out[p.first] = w.sent_to(p)
        out[p.name] = out[p.first]
    for g in w.groups.values():
        out[g.name] = w.sent_to(g)
    return out


def _classify(w: World, trial: Trial) -> str:
    """Why a trial failed — a model-selection signal, not just a label.
    Order matters: provider trouble and format breakage explain a
    behavioural miss, not the other way round."""
    errors = trial.flags.get("_llm_errors") or ""
    turns = w.main_turns
    if any(t.error is not None for t in turns) or (
            errors and _PROVIDER_ERR.search(errors)):
        return "provider_error"
    tools_offered = set().union(*(set(t.tools) for t in turns)) if turns else set()
    for t in turns:
        for c in t.tool_calls:
            if c.get("hosted"):
                continue  # server-side tool (web_search) — never in the offered list
            if tools_offered and c["name"] not in tools_offered:
                return "tool_format"
            args = c.get("arguments")
            if isinstance(args, str) and args.strip():
                try:
                    import json
                    json.loads(args)
                except ValueError:
                    return "tool_format"
        if t.result and _LEAKED_TOOL_TEXT.search(t.result):
            return "tool_format"
        # Schema violations (invented/missing params) count only when the
        # tool never succeeded afterwards — a recovered retry isn't the cause.
        for i, e in enumerate(t.tool_events):
            if e["output"].startswith("[raised] TypeError") and "argument" in e["output"]:
                if not any(later["name"] == e["name"]
                           and not later["output"].startswith("[raised]")
                           for later in t.tool_events[i + 1:]):
                    return "tool_format"
    if turns and all(not (t.result or "").strip() and not t.tool_calls
                     for t in turns) and not w.deliveries:
        return "empty_response"
    return "behavioural"


async def _usage(w: World, trial: Trial) -> None:
    from server.repositories.llm_call_log import LlmCallLogRepository
    from server.services import model_registry
    try:
        rows = await LlmCallLogRepository(w.db).usage_by_model()
    except Exception:
        return
    pricing = model_registry.pricing(w.ctx.settings.config_dir)
    cost, priced = 0.0, True
    errors = []
    for r in rows:
        trial.prompt_tokens += int(r["prompt_tokens"] or 0)
        trial.completion_tokens += int(r["completion_tokens"] or 0)
        trial.llm_calls += int(r["calls"] or 0)
        if r.get("errors"):
            errors.append(r["errors"])
        rates = pricing.get(r["model"])
        if rates is None:
            priced = False
            continue
        rin, rout = rates
        cached = int(r["cached_tokens"] or 0)
        cost += ((int(r["prompt_tokens"] or 0) - cached) * rin + cached * rin * 0.1
                 + int(r["completion_tokens"] or 0) * rout) / 1_000_000
    trial.cost_usd = round(cost, 5) if priced and rows else None
    if errors:
        trial.flags["_llm_errors"] = " | ".join(errors)[:2000]


def _trial_row(t: Trial) -> dict[str, Any]:
    return {
        "case_id": t.case_id, "category": t.category, "passed": t.passed,
        "response": t.response, "latency": round(t.latency, 2),
        "judge_score": t.judge.score if t.judge else None,
        "judge_reasoning": t.judge.reasoning if t.judge else None,
        "checks": [{"kind": c.check.kind, "params": c.check.params,
                    "passed": c.passed, "detail": c.detail} for c in t.checks],
        "transcript": t.transcript[-200000:], "error": t.error,
        "repeat_index": t.repeat_index, "failure_class": t.failure_class,
        "prompt_tokens": t.prompt_tokens, "completion_tokens": t.completion_tokens,
        "cost_usd": t.cost_usd, "llm_calls": t.llm_calls,
        "judge_items": ([{"item": v.item, "passed": v.passed, "why": v.why}
                         for v in t.judge.verdicts] if t.judge else None),
        "tool_calls": t.tool_calls, "deliveries": t.deliveries,
        "flags": t.flags,
    }
