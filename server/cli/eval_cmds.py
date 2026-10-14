"""Bob CLI eval subapp (docs/evals.md)."""

from __future__ import annotations

from server.cli._helpers import *  # noqa: F403,F405


app = typer.Typer(help="Eval suite: discipline + capability tiers in throwaway worlds")


@app.command("list")
def eval_list(
    category: Annotated[Optional[str], typer.Option("--category", "-c")] = None,
    tier: Annotated[Optional[str], typer.Option("--tier")] = None,
) -> None:
    """List eval cases."""
    from server.evals.registry import select
    cases = select(category=category, tier=tier)
    typer.echo(f"{'ID':<46} {'TIER':<11} {'CATEGORY':<20} CHECKS/RUBRIC")
    for c in cases:
        typer.echo(f"{c.id:<46} {c.tier:<11} {c.category:<20} "
                   f"{len(c.checks)}/{len(c.rubric)}")
    typer.echo(f"\n{len(cases)} case(s)")


@app.command("run")
def eval_run(
    category: Annotated[Optional[str], typer.Option("--category", "-c")] = None,
    case_ids: Annotated[Optional[list[str]], typer.Option(
        "--case", help="Case id (repeatable)")] = None,
    tier: Annotated[Optional[str], typer.Option(
        "--tier", help="discipline | capability")] = None,
    repeats: Annotated[int, typer.Option(
        "--repeats", "-n", help="Trials per case (3 default; 5+ for "
        "per-case verdicts)")] = 3,
    concurrency: Annotated[int, typer.Option("--concurrency", "-j")] = 3,
    model: Annotated[Optional[str], typer.Option(
        "--model", help="Pin the model for every Bob turn (slug or alias)")] = None,
    effort: Annotated[Optional[str], typer.Option(
        "--effort", help="Reasoning effort override for Bob turns")] = None,
    judge_model: Annotated[Optional[str], typer.Option("--judge-model")] = None,
    judge_effort: Annotated[Optional[str], typer.Option(
        "--judge-effort", help="Reasoning effort for the judge model")] = None,
    skip_judge: Annotated[bool, typer.Option("--skip-judge")] = False,
    notes: Annotated[str, typer.Option("--notes")] = "",
    verbose: Annotated[bool, typer.Option("--verbose", "-v",
                                          help="Show server warnings")] = False,
) -> None:
    """Run cases in throwaway worlds; results go to the eval store."""
    import asyncio
    import logging
    if not verbose:
        for name in ("server", "httpx", "openai", "mcp"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
    asyncio.run(_eval_run(category, case_ids, tier, repeats, concurrency,
                          model, effort, judge_model, judge_effort, skip_judge, notes))


async def _eval_run(category, case_ids, tier, repeats, concurrency, model,
                    effort, judge_model, judge_effort, skip_judge, notes) -> None:
    from server.evals import report
    from server.evals.registry import select
    from server.evals.runner import EvalRunner, RunConfig, open_store
    from server.evals.store import EvalStore
    from server.services import model_registry

    settings = Settings.from_env()
    if model:
        model = model_registry.resolve(model, settings.config_dir)
    cases = select(category=category, case_ids=case_ids, tier=tier)
    if not cases:
        typer.echo("No eval cases matched.")
        raise SystemExit(2)
    cfg = RunConfig(model=model, effort=effort, judge_model=judge_model,
                    judge_effort=judge_effort,
                    repeats=max(1, repeats), concurrency=max(1, concurrency),
                    category=category, case_ids=case_ids, tier=tier,
                    skip_judge=skip_judge, notes=notes,
                    progress=lambda s: typer.echo(s))
    result = await EvalRunner(settings).run(cases, cfg)
    db, _ = await open_store(settings)
    try:
        store = EvalStore(db)
        run = await store.get_run(result.run_id)
        if run is None:
            raise typer.Exit(code=1)
        typer.echo("")
        typer.echo(report.summary(run, await store.trials(result.run_id)))
    finally:
        await db.close()


@app.command("history")
def eval_history(limit: Annotated[int, typer.Option("--limit", "-n")] = 15) -> None:
    """Recent runs."""
    import asyncio
    asyncio.run(_history(limit))


async def _history(limit: int) -> None:
    from server.evals.runner import open_store
    from server.evals.store import EvalStore
    db, _ = await open_store(Settings.from_env())
    try:
        rows = await EvalStore(db).runs(limit)
        if not rows:
            typer.echo("No eval runs found.")
            return
        typer.echo(f"{'RUN':<9} {'STARTED':<17} {'MODEL':<28} {'REP':>3} "
                   f"{'TRIALS':>9} {'RATE':>5} {'COST':>7}  FILTER")
        for r in rows:
            rate = f"{r['overall_pass_rate']:.0%}" if r["overall_pass_rate"] is not None else "—"
            cost = f"${r['total_cost_usd']:.2f}" if r.get("total_cost_usd") is not None else "—"
            filt = r.get("case_filter") or r.get("category") or ("legacy" if r.get("notes", "") and "imported" in r["notes"] else "all")
            typer.echo(f"{r['id'][:8]:<9} {(r['started_at'] or '')[:16]:<17} "
                       f"{(r.get('model') or '?')[:28]:<28} {r.get('repeats') or 1:>3} "
                       f"{r['passed_cases']:>4}/{r['total_cases']:<4} {rate:>5} {cost:>7}  {filt}")
    finally:
        await db.close()


@app.command("show")
def eval_show(
    run_ref: Annotated[str, typer.Argument(help="Run id or prefix")],
    case: Annotated[Optional[str], typer.Option(
        "--case", help="Print each trial's checks, rubric verdicts and transcript")] = None,
) -> None:
    """A run's summary (or one case's trials in full)."""
    import asyncio
    asyncio.run(_show(run_ref, case))


async def _show(run_ref: str, case: str | None) -> None:
    import json as _json

    from server.evals import report
    from server.evals.runner import open_store
    from server.evals.store import EvalStore
    db, _ = await open_store(Settings.from_env())
    try:
        store = EvalStore(db)
        run = await store.get_run(run_ref)
        if run is None:
            typer.echo(f"No unique run matches {run_ref!r}")
            raise SystemExit(2)
        trials = await store.trials(run["id"])
        if not case:
            typer.echo(report.summary(run, trials))
            return
        for t in (t for t in trials if t["case_id"] == case):
            typer.echo(f"\n##### {case} #{t['repeat_index'] + 1}: "
                       f"{'PASS' if t['passed'] else 'FAIL'} "
                       f"[{t.get('failure_class') or ''}] "
                       f"{t['llm_latency_seconds'] or 0:.0f}s")
            for c in _json.loads(t["structural_results_json"] or "[]"):
                typer.echo(f"  check {c['kind']}: {'ok' if c['passed'] else 'FAIL ' + c.get('detail', '')}")
            if t.get("judge_reasoning"):
                typer.echo("  " + t["judge_reasoning"].replace("\n", "\n  "))
            if t.get("error_message"):
                typer.echo(f"  error: {t['error_message']}")
            body = t.get("input_messages_json") or ""
            try:
                body = _json.loads(body)
            except ValueError:
                pass
            typer.echo(body if isinstance(body, str) else _json.dumps(body, indent=1)[:20000])
    finally:
        await db.close()


@app.command("compare")
def eval_compare(
    run_a: Annotated[str, typer.Argument()],
    run_b: Annotated[str, typer.Argument()],
) -> None:
    """Per-category deltas with 95% CIs + per-case flips (Fisher exact)."""
    import asyncio
    asyncio.run(_compare(run_a, run_b))


async def _compare(a_ref: str, b_ref: str) -> None:
    from server.evals import report
    from server.evals.runner import open_store
    from server.evals.store import EvalStore
    db, _ = await open_store(Settings.from_env())
    try:
        store = EvalStore(db)
        a, b = await store.get_run(a_ref), await store.get_run(b_ref)
        if a is None or b is None:
            typer.echo("Both runs must resolve to a unique run id/prefix.")
            raise SystemExit(2)
        typer.echo(report.compare(a, await store.trials(a["id"]),
                                  b, await store.trials(b["id"])))
    finally:
        await db.close()


@app.command("judge-calibrate")
def eval_judge_calibrate(
    path: Annotated[Optional[Path], typer.Option(
        "--file", help="Labelled set (default ~/data/evals/calibration.jsonl)")] = None,
    judge_model: Annotated[Optional[str], typer.Option("--judge-model")] = None,
    judge_effort: Annotated[Optional[str], typer.Option("--judge-effort")] = None,
    show: Annotated[bool, typer.Option("--show", help="Print disagreements")] = True,
) -> None:
    """Judge agreement with hand labels (overall + per item, Cohen's κ)."""
    import asyncio
    asyncio.run(_calibrate(path, judge_model, judge_effort, show))


async def _calibrate(path: Path | None, judge_model: str | None,
                     judge_effort: str | None, show: bool) -> None:
    from server.evals.calibration import calibrate, default_path, load
    from server.evals.runner import open_store
    settings = Settings.from_env()
    path = path or default_path(settings)
    entries = load(path)
    if not entries:
        typer.echo(f"No labelled entries in {path} (see docs/evals.md → Judge calibration).")
        raise SystemExit(2)
    db, ctx = await open_store(settings)
    try:
        typer.echo(await calibrate(ctx, entries, judge_model=judge_model,
                                   judge_effort=judge_effort, show=show,
                                   progress=lambda s: typer.echo(s)))
    finally:
        await db.close()


@app.command("calibration-draft")
def eval_calibration_draft(
    out: Annotated[Optional[Path], typer.Option("--out")] = None,
    per_case: Annotated[int, typer.Option("--per-case")] = 2,
    limit: Annotated[int, typer.Option("--limit")] = 40,
) -> None:
    """Draft unlabelled calibration entries from stored trials (label them
    by hand, then move into calibration.jsonl)."""
    import asyncio
    asyncio.run(_calibration_draft(out, per_case, limit))


async def _calibration_draft(out: Path | None, per_case: int, limit: int) -> None:
    from server.evals.calibration import default_path, draft_entries
    from server.evals.runner import open_store
    settings = Settings.from_env()
    db, _ = await open_store(settings)
    try:
        out = out or default_path(settings).with_name("calibration.draft.jsonl")
        n = await draft_entries(db, out, per_case=per_case, limit=limit)
        typer.echo(f"wrote {n} draft entr{'y' if n == 1 else 'ies'} to {out}")
    finally:
        await db.close()


@app.command("snapshot")
def eval_snapshot(
    mcp: Annotated[bool, typer.Option("--mcp/--no-mcp",
                                      help="List live MCP servers' tools")] = True,
) -> None:
    """Capture production context for worlds (read-only): Bob's self-brief
    claims, the live skills' SKILL.md files, global MCP tool lists."""
    import asyncio
    asyncio.run(_snapshot(mcp))


async def _snapshot(mcp: bool) -> None:
    from server.evals.snapshot import take_snapshot
    typer.echo(await take_snapshot(Settings.from_env(), include_mcp=mcp))


@app.command("import-legacy")
def eval_import_legacy() -> None:
    """Copy pre-v2 runs/results out of bob.db (read-only) into the eval store."""
    import asyncio
    asyncio.run(_import_legacy())


async def _import_legacy() -> None:
    import sqlite3

    from server.evals.runner import open_store
    from server.evals.store import EvalStore
    settings = Settings.from_env()
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        from server.evals.store import read_legacy
        runs, results = read_legacy(conn)
    finally:
        conn.close()
    db, _ = await open_store(settings)
    try:
        n_runs, n_res = await EvalStore(db).insert_legacy(runs, results)
        typer.echo(f"imported {n_runs} run(s), {n_res} trial row(s)")
    finally:
        await db.close()
