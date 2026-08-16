"""Bob CLI dream subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405

app = typer.Typer(help="Dream system: reflective self-improvement and proactive plans")


async def _run(trigger: str, dry_run: bool) -> None:
    from bob_server.config import Settings
    from bob_server.context import AppContext
    from bob_server.database import Database
    from pathlib import Path

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent.parent / "schemas"
    db = Database(settings.db_path or Path("bob.db"), schema_dir)
    await db.connect()
    await db.apply_migrations()
    ctx = AppContext(settings=settings, db=db)
    try:
        if dry_run:
            from bob_server.services.dream import DreamStore

            store = DreamStore(ctx)
            await store.sweep_stale_runs()
            due = await store.sessions_due(
                min_new_messages=settings.dream.min_new_messages_per_session,
                max_sessions=settings.dream.max_sessions_per_run,
                first_run_lookback_days=settings.dream.first_run_lookback_days,
            )
            typer.echo(f"Due sessions ({len(due)}):")
            for s in due:
                typer.echo(f"  {s['session_key']}  new_messages={s['new_messages']} newest={s['newest_message_at']}")
            return
        from bob_server.services.dream import DreamRunner

        result = await DreamRunner(ctx).maybe_run(trigger=trigger)
        if result is None:
            typer.echo("Dream not due (gated) or already running.")
            return
        stats = result["stats"]
        typer.echo(f"Dream run {result['run_id']} complete:")
        typer.echo(f"  sessions reviewed: {len(stats.get('sessions', []))}")
        typer.echo(f"  resolutions created: {len(stats.get('resolutions_created', []))}")
        typer.echo(f"  plans created: {len(stats.get('plans_created', []))}")
        typer.echo(f"  merged: {len(stats.get('merged', []))}  suppressed: {len(stats.get('suppressed', []))}")
    finally:
        await db.close()


@app.command("run")
def dream_run(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show due sessions without running")] = False,
) -> None:
    """Run a dream now (bypasses the enabled flag)."""
    import asyncio

    asyncio.run(_run("cli", dry_run))


@app.command("status")
def dream_status() -> None:
    """Show dream settings and the last runs."""
    import asyncio

    asyncio.run(_status())


async def _status() -> None:
    from bob_server.config import Settings
    from bob_server.context import AppContext
    from bob_server.database import Database
    from pathlib import Path

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent.parent / "schemas"
    db = Database(settings.db_path or Path("bob.db"), schema_dir)
    await db.connect()
    ctx = AppContext(settings=settings, db=db)
    try:
        from bob_server.services.dream import DreamStore
        from bob_server.services.dream import config as dream_config

        d = settings.dream
        sessions = await dream_config.list_autoplan_sessions(db, enabled=True)
        typer.echo(f"enabled={d.enabled} interval_minutes={d.interval_minutes} draft_mode={d.draft_mode}")
        typer.echo(f"autoplan ON for {len(sessions)} session(s)" + (": " + ", ".join(s["session_key"] for s in sessions) if sessions else ""))
        typer.echo(f"caps: sessions/run={d.max_sessions_per_run} new_items/type={d.max_new_items_per_type}")
        store = DreamStore(ctx)
        for run in await store.list_runs(5):
            typer.echo(f"  {run['id']} {run['status']} trigger={run['trigger']} started={run['started_at']}")
    finally:
        await db.close()


@app.command("autoplan")
def dream_autoplan(
    state: Annotated[Optional[str], typer.Argument(help="on | off | (blank to list)")] = None,
    session_key: Annotated[Optional[str], typer.Option("--session", help="Session key to toggle (required for on/off)")] = None,
) -> None:
    """Session-scoped autoplan: toggle one session, or list sessions with it on."""
    import asyncio

    asyncio.run(_autoplan(state, session_key))


async def _autoplan(state: str | None, session_key: str | None) -> None:
    from bob_server.config import Settings
    from bob_server.database import Database
    from bob_server.services.dream import config as dream_config
    from pathlib import Path

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent.parent / "schemas"
    db = Database(settings.db_path or Path("bob.db"), schema_dir)
    await db.connect()
    await db.apply_migrations()
    try:
        if state is not None:
            val = state.strip().lower()
            if val not in ("on", "off"):
                typer.echo("Usage: bob dream autoplan [on|off] --session <session_key>")
                raise typer.Exit(1)
            if not session_key:
                typer.echo("autoplan is session-scoped: pass --session <session_key> (list keys with bare 'autoplan')")
                raise typer.Exit(1)
            ok = await dream_config.set_session_autoplan(db, session_key, val == "on")
            if not ok:
                typer.echo(f"No active session route for {session_key}")
                raise typer.Exit(1)
        sessions = await dream_config.list_autoplan_sessions(db, enabled=True)
        if sessions:
            typer.echo("autoplan ON for:")
            for s in sessions:
                typer.echo(f"  {s['session_key']}")
        else:
            typer.echo("autoplan OFF everywhere (no sessions enabled)")
    finally:
        await db.close()


@app.command("reindex")
def dream_reindex() -> None:
    """Re-embed all active dream items (needed after embedding-metric changes)."""
    import asyncio

    asyncio.run(_reindex())


async def _reindex() -> None:
    from bob_server.config import Settings
    from bob_server.context import AppContext
    from bob_server.database import Database
    from pathlib import Path

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent.parent / "schemas"
    db = Database(settings.db_path or Path("bob.db"), schema_dir)
    await db.connect()
    await db.apply_migrations()
    ctx = AppContext(settings=settings, db=db)
    try:
        from bob_server.services.dream import DreamStore

        count = await DreamStore(ctx).rebuild_item_embeddings()
        typer.echo(f"re-embedded {count} item(s)")
    finally:
        await db.close()


@app.command("list")
def dream_list(
    kind: Annotated[str, typer.Option("--kind", help="plans | resolutions")] = "plans",
    status: Annotated[Optional[str], typer.Option("--status")] = None,
) -> None:
    """List dream plans or resolutions."""
    import asyncio

    asyncio.run(_list(kind, status))


async def _list(kind: str, status: str | None) -> None:
    from bob_server.config import Settings
    from bob_server.context import AppContext
    from bob_server.database import Database
    from pathlib import Path

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent.parent / "schemas"
    db = Database(settings.db_path or Path("bob.db"), schema_dir)
    await db.connect()
    ctx = AppContext(settings=settings, db=db)
    try:
        from bob_server.services.dream import DreamStore

        store = DreamStore(ctx)
        statuses = [status] if status else None
        if kind == "resolutions":
            for r in await store.list_resolutions(statuses):
                typer.echo(f"  {r['id']} [{r['status']}] {r['title']} (observed {r['observation_count']}x, last {r['last_seen_at']})")
        else:
            for p in await store.list_plans(statuses):
                typer.echo(f"  {p['id']} [{p['status']}] {p['title']} — {p['proposed_action']}")
    finally:
        await db.close()
