"""Bob CLI eval subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405


app = typer.Typer(help="LLM eval framework")



@app.command("list")
def eval_list(
    category: Annotated[Optional[str], typer.Option("--category", "-c")] = None,
) -> None:
    """List available eval cases."""
    import asyncio
    asyncio.run(_eval_list(category))


async def _eval_list(category: str | None) -> None:
    from bob_server.evals.registry import get_all_cases, get_cases_by_category
    cases = get_cases_by_category(category) if category else get_all_cases()
    if not cases:
        typer.echo("No eval cases found.")
        return
    typer.echo(f"{'ID':<40} {'Category':<20} Description")
    typer.echo("-" * 90)
    for c in cases:
        typer.echo(f"{c.id:<40} {c.category:<20} {c.description}")


@app.command("run")
def eval_run(
    category: Annotated[Optional[str], typer.Option("--category", "-c")] = None,
    case_id: Annotated[Optional[str], typer.Option("--case")] = None,
    threshold: Annotated[float, typer.Option("--threshold", "-t")] = 0.7,
    skip_judge: Annotated[bool, typer.Option("--skip-judge")] = False,
) -> None:
    """Run eval cases against live LLM APIs."""
    import asyncio
    asyncio.run(_eval_run(category, case_id, threshold, skip_judge))


async def _eval_run(
    category: str | None,
    case_id: str | None,
    threshold: float,
    skip_judge: bool,
) -> None:
    from bob_server.config import Settings
    from bob_server.context import AppContext
    from bob_server.database import Database

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent / "schemas"
    db_path = settings.db_path or Path("bob.db")
    db = Database(db_path, schema_dir)
    await db.connect()
    ctx = AppContext(settings=settings, db=db)

    try:
        from bob_server.evals.runner import EvalRunner
        runner = EvalRunner(ctx)
        results = await runner.run_all(
            category=category,
            case_id=case_id,
            judge_threshold=threshold,
            skip_judge=skip_judge,
        )

        if not results:
            typer.echo("No eval cases matched.")
            return

        typer.echo(f"\n{'ID':<35} {'PASS':<6} {'Struct':<8} {'Judge':<8} Latency")
        typer.echo("-" * 75)
        for r in results:
            struct_pass = sum(1 for s in r.structural_results if s.passed)
            struct_total = len(r.structural_results)
            judge_str = f"{r.judge_result.overall:.1f}" if r.judge_result else "skip"
            status = "PASS" if r.passed else "FAIL"
            typer.echo(
                f"{r.case_id:<35} {status:<6} "
                f"{struct_pass}/{struct_total:<6} {judge_str:<8} "
                f"{r.llm_latency_seconds:.1f}s"
            )
            if r.error_message:
                typer.echo(f"  Error: {r.error_message}")

        passed = sum(1 for r in results if r.passed)
        typer.echo(f"\n{passed}/{len(results)} passed")

        if passed < len(results):
            raise SystemExit(1)
    finally:
        await db.close()


@app.command("history")
def eval_history(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
) -> None:
    """Show historical eval run results."""
    import asyncio
    asyncio.run(_eval_history(limit))


async def _eval_history(limit: int) -> None:
    from bob_server.config import Settings
    from bob_server.database import Database

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent / "schemas"
    db_path = settings.db_path or Path("bob.db")
    db = Database(db_path, schema_dir)
    await db.connect()
    try:
        rows = await db.fetch_all(
            "SELECT * FROM eval_runs WHERE status='completed' "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        if not rows:
            typer.echo("No eval runs found.")
            return
        typer.echo(f"{'Run ID':<38} {'Started':<22} {'Cat':<15} {'Pass':>5}/{'<5'} Rate")
        typer.echo("-" * 95)
        for r in rows:
            ts = r["started_at"][:19].replace("T", " ")
            cat = r.get("category") or "all"
            rate = f"{r['overall_pass_rate']:.0%}" if r["overall_pass_rate"] else "N/A"
            typer.echo(
                f"{r['id']:<38} {ts:<22} {cat:<15} "
                f"{r['passed_cases']:>5}/{r['total_cases']:<5} {rate}"
            )
    finally:
        await db.close()


# ============================================================================
# WhatsApp commands
# ============================================================================
