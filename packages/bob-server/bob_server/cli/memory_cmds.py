"""Bob CLI memory subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405


app = typer.Typer(help="Memory wiki operations")



@app.command("seed")
def memory_seed(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be processed without calling LLM")] = False,
) -> None:
    """Regenerate memory from all session history using the bulletin generator."""
    import asyncio
    asyncio.run(_memory_seed(dry_run))


async def _memory_seed(dry_run: bool) -> None:
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
        from bob_server.services.memory.seed import seed_from_history

        workspace = settings.harness.workspace_dir
        result = await seed_from_history(ctx, workspace, dry_run=dry_run)

        typer.echo(f"\nSeed result:")
        typer.echo(f"  Sessions processed: {result.get('sessions_processed', 0)}")
        typer.echo(f"  Bulletins generated: {result.get('bulletins_generated', 0)}")
        typer.echo(f"  Bulletins skipped: {result.get('bulletins_skipped', 0)}")
        typer.echo(f"  Errors: {len(result.get('errors', []))}")
    finally:
        await db.close()


@app.command("seed-email")
def memory_seed_email(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be processed without calling LLM")] = False,
    thread_id: Annotated[Optional[str], typer.Option("--thread", help="Process a specific email thread by agentmail_thread_id")] = None,
) -> None:
    """Regenerate memory from email thread history using the bulletin generator."""
    import asyncio
    asyncio.run(_memory_seed_email(dry_run, thread_id))


async def _memory_seed_email(dry_run: bool, thread_id: str | None) -> None:
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
        from bob_server.services.memory.seed_email import seed_from_email_history

        workspace = settings.harness.workspace_dir
        result = await seed_from_email_history(ctx, workspace, dry_run=dry_run, thread_id=thread_id)

        typer.echo(f"\nSeed-email result:")
        typer.echo(f"  Threads processed: {result.get('threads_processed', 0)}")
        typer.echo(f"  Bulletins generated: {result.get('bulletins_generated', 0)}")
        typer.echo(f"  Bulletins skipped: {result.get('bulletins_skipped', 0)}")
        typer.echo(f"  Errors: {len(result.get('errors', []))}")
    finally:
        await db.close()


@app.command("seed-manual")
def memory_seed_manual(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be processed without writing")] = False,
) -> None:
    """Replay memory_write tool calls from LLM logs as bulletins."""
    import asyncio
    asyncio.run(_memory_seed_manual(dry_run))


async def _memory_seed_manual(dry_run: bool) -> None:
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
        from bob_server.services.memory.seed_manual import seed_manual_bulletins

        workspace = settings.harness.workspace_dir
        result = await seed_manual_bulletins(ctx, workspace, dry_run=dry_run)

        typer.echo(f"\nSeed-manual result:")
        typer.echo(f"  Log rows scanned: {result.get('log_rows_scanned', 0)}")
        typer.echo(f"  Bulletins generated: {result.get('bulletins_generated', 0)}")
        typer.echo(f"  Errors: {len(result.get('errors', []))}")
    finally:
        await db.close()


@app.command("rebuild")
def memory_rebuild(
    all: Annotated[bool, typer.Option("--all", help="Rebuild all derived data from bulletins")] = False,
    entity_id: Annotated[Optional[str], typer.Option("--entity", help="Rebuild indexes for a specific entity")] = None,
    full: Annotated[bool, typer.Option("--full", help="Use full-document mode instead of patches")] = False,
) -> None:
    """Rebuild memory indexes and derived data from bulletins."""
    import asyncio
    asyncio.run(_memory_rebuild(all, entity_id, full))


async def _memory_rebuild(all: bool, entity_id: str | None, full: bool) -> None:
    from bob_server.config import Settings
    from bob_server.context import AppContext
    from bob_server.database import Database

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent / "schemas"
    db_path = settings.db_path or Path("bob.db")
    db = Database(db_path, schema_dir)
    await db.connect()
    await db.apply_migrations()
    ctx = AppContext(settings=settings, db=db)

    try:
        from bob_server.services.memory import MemoryService

        workspace = settings.harness.workspace_dir
        svc = MemoryService(ctx)

        result = await svc.rebuild(workspace, entity_id=entity_id, all=all)
        typer.echo(f"Rebuild result: {json.dumps(result, indent=2)}")
    finally:
        await db.close()


@app.command("reconcile")
def memory_reconcile(
    entity_ids: Annotated[Optional[list[str]], typer.Argument(help="Entity IDs to reconcile")] = None,
    all: Annotated[bool, typer.Option("--all", help="Reconcile all active entities")] = False,
    render_only: Annotated[bool, typer.Option("--render", help="Just show the full render, don't reconcile")] = False,
) -> None:
    """Run entity reconciliation to detect and fix inconsistencies."""
    import asyncio
    asyncio.run(_memory_reconcile(entity_ids, all, render_only))


@app.command("supplement")
def memory_supplement(
    entity_ids: Annotated[list[str], typer.Argument(help="Entity IDs to supplement with missing claims")],
) -> None:
    """Gap-fill: re-extract from source bulletins, only write missing claims."""
    import asyncio
    asyncio.run(_memory_supplement(entity_ids))


async def _memory_reconcile(entity_ids: list[str] | None, all: bool, render_only: bool) -> None:
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
        from bob_server.services.memory.reconciliation import render_entity_full, reconcile_entity
        from bob_server.services.llm_dispatch import LLMDispatchService

        if all:
            rows = await db.fetch_all(
                "SELECT entity_id FROM memory_entities WHERE status = 'active'"
            )
            entity_ids = [r["entity_id"] for r in rows]

        if not entity_ids:
            typer.echo("No entity IDs specified. Use --all or provide entity IDs.")
            return

        llm = LLMDispatchService(ctx)

        for eid in entity_ids:
            if render_only:
                rendered = await render_entity_full(db, eid)
                typer.echo(f"\n{'='*60}")
                typer.echo(f"  {eid}")
                typer.echo(f"{'='*60}")
                typer.echo(rendered)
            else:
                typer.echo(f"Reconciling {eid}...")
                result = await reconcile_entity(db, llm, eid, settings=settings)
                typer.echo(json.dumps(result, indent=2, default=str))
    finally:
        await db.close()


@app.command("model-override-set")
def memory_model_override_set(
    entity_id: Annotated[str, typer.Argument(help="Entity ID to override")],
    model: Annotated[str, typer.Argument(help="Model name (e.g. gpt-5.5, o3)")],
    reason: Annotated[str, typer.Option("--reason", help="Why this override exists")] = "",
) -> None:
    """Set a per-entity model override for reconciliation."""
    import asyncio
    asyncio.run(_memory_model_override_set(entity_id, model, reason))


@app.command("model-override-remove")
def memory_model_override_remove(
    entity_id: Annotated[str, typer.Argument(help="Entity ID to remove override for")],
) -> None:
    """Remove a per-entity model override."""
    import asyncio
    asyncio.run(_memory_model_override_remove(entity_id))


@app.command("model-override-list")
def memory_model_override_list() -> None:
    """List all per-entity model overrides."""
    import asyncio
    asyncio.run(_memory_model_override_list())


async def _memory_model_override_set(entity_id: str, model: str, reason: str) -> None:
    from bob_server.config import Settings
    from bob_server.database import Database

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent / "schemas"
    db_path = settings.db_path or Path("bob.db")
    db = Database(db_path, schema_dir)
    await db.connect()

    try:
        await db.execute(
            "INSERT OR REPLACE INTO recon_model_overrides (entity_id, model, reason) "
            "VALUES (?, ?, ?)",
            (entity_id, model, reason),
        )
        typer.echo(f"Set override: {entity_id} → {model}" + (f" ({reason})" if reason else ""))
    finally:
        await db.close()


async def _memory_model_override_remove(entity_id: str) -> None:
    from bob_server.config import Settings
    from bob_server.database import Database

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent / "schemas"
    db_path = settings.db_path or Path("bob.db")
    db = Database(db_path, schema_dir)
    await db.connect()

    try:
        await db.execute(
            "DELETE FROM recon_model_overrides WHERE entity_id = ?", (entity_id,)
        )
        typer.echo(f"Removed override for {entity_id}")
    finally:
        await db.close()


async def _memory_model_override_list() -> None:
    from bob_server.config import Settings
    from bob_server.database import Database

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent / "schemas"
    db_path = settings.db_path or Path("bob.db")
    db = Database(db_path, schema_dir)
    await db.connect()

    try:
        rows = await db.fetch_all(
            "SELECT entity_id, model, reason, set_at "
            "FROM recon_model_overrides ORDER BY set_at DESC"
        )
        if not rows:
            typer.echo("(no overrides)")
            return
        for r in rows:
            typer.echo(f"{r['entity_id']}\t{r['model']}\t{r['set_at']}" + (f"\t{r['reason']}" if r['reason'] else ""))
    finally:
        await db.close()


async def _memory_supplement(entity_ids: list[str]) -> None:
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
        from bob_server.services.memory import MemoryService

        workspace = settings.harness.workspace_dir
        svc = MemoryService(ctx)

        for eid in entity_ids:
            typer.echo(f"Supplementing {eid}...")
            result = await svc.supplement_entity(workspace, entity_id=eid)
            typer.echo(json.dumps(result, indent=2, default=str))
    finally:
        await db.close()


@app.command("merge")
def memory_merge(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview merges without executing")] = False,
) -> None:
    """Detect and merge duplicate entities using embeddings + LLM."""
    import asyncio
    asyncio.run(_memory_merge(dry_run))


async def _memory_merge(dry_run: bool) -> None:
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
        from bob_server.services.memory import MemoryService

        svc = MemoryService(ctx)
        result = await svc.merge_entities(dry_run=dry_run)
        typer.echo(json.dumps(result, indent=2, default=str))
    finally:
        await db.close()


@app.command("validate")
def memory_validate() -> None:
    """Validate memory structure: check frontmatter, dangling refs, required fields."""
    import asyncio
    asyncio.run(_memory_validate())


async def _memory_validate() -> None:
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
        from bob_server.services.memory import MemoryService

        workspace = settings.harness.workspace_dir
        svc = MemoryService(ctx)

        result = await svc.validate(workspace)
        if result["valid"]:
            typer.echo("Memory is valid.")
        else:
            typer.echo(f"Issues found ({len(result['issues'])}):")
            for issue in result["issues"]:
                typer.echo(f"  - {issue}")
    finally:
        await db.close()


@app.command("reindex")
def memory_reindex() -> None:
    """Rebuild the FTS search index from existing entity data (no LLM calls)."""
    import asyncio
    asyncio.run(_memory_reindex())


async def _memory_reindex() -> None:
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
        from bob_server.services.memory import MemoryService
        svc = MemoryService(ctx)
        count = await svc.rebuild_fts()
        typer.echo(f"FTS index rebuilt: {count} entities indexed.")
    finally:
        await db.close()


@app.command("cleanup-contacts")
def memory_cleanup_contacts(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change without writing")] = False,
) -> None:
    """Remove duplicate contact entities and rewire references to canonical IDs."""
    import asyncio
    asyncio.run(_memory_cleanup_contacts(dry_run))


async def _memory_cleanup_contacts(dry_run: bool) -> None:
    from bob_server.config import Settings
    from bob_server.context import AppContext
    from bob_server.database import Database
    from bob_server.services.memory.cleanup import run_cleanup, build_renaming_map
    from bob_server.services.memory.contact_directory import ContactDirectory

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent / "schemas"
    db_path = settings.db_path or Path("bob.db")
    db = Database(db_path, schema_dir)
    await db.connect()

    try:
        workspace = settings.harness.workspace_dir
        memory_dir = workspace / "memory"
        directory = await ContactDirectory.load(db)

        typer.echo(f"Loaded {len(directory.all_canonical_ids())} contacts from DB")

        if dry_run:
            rename, merge = await build_renaming_map(db, directory)
            typer.echo(f"\n[Dry run] Would rename {len(rename)} entities")
            typer.echo(f"[Dry run] Would merge {len(merge)} duplicates into canonical entities")
            for old, new in sorted(rename.items()):
                typer.echo(f"  {old} -> {new}")
            return

        result = await run_cleanup(db, directory, dry_run=False)
        typer.echo("\nCleanup result:")
        typer.echo(f"  Renamed: {result['renamed']}")
        typer.echo(f"  Merged:  {result['merged']}")
        typer.echo(f"  Deleted: {result['deleted']}")
        typer.echo(f"  Rewritten claims:     {result['rewritten_claims']}")
        typer.echo(f"  Rewritten bulletins:  {result['rewritten_bulletins']}")
        typer.echo(f"  Rewritten related:    {result['rewritten_related']}")
        typer.echo(f"  Enriched with DB FK:  {result['enriched']}")
    finally:
        await db.close()


@app.command("query")
def memory_query(
    question: Annotated[str, typer.Argument(help="Question to search memory for")],
    entity_type: Annotated[str, typer.Option("--type", help="Filter to entity type")] = "",
    actor: Annotated[Optional[str], typer.Option("--actor", help="Actor contact ID")] = None,
    channel: Annotated[Optional[str], typer.Option("--channel", help="Channel context")] = None,
) -> None:
    """Query memory with a natural language question."""
    import asyncio
    asyncio.run(_memory_query(question, entity_type, actor, channel))


async def _memory_query(question: str, entity_type: str, actor: str | None, channel: str | None) -> None:
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
        from bob_server.services.memory import MemoryService

        workspace = settings.harness.workspace_dir
        svc = MemoryService(ctx)

        result = await svc.search_entries(workspace, question, entity_type=entity_type or "")
        typer.echo(f"\nAbstract: {result.get('abstract', '')}")
        typer.echo(f"\nResults ({len(result.get('results', []))}):")
        for r in result.get("results", []):
            typer.echo(f"  - {r.get('entity_id', '')} ({r.get('entity_type', '')})")
            if r.get("relevance"):
                typer.echo(f"    {r['relevance']}")
    finally:
        await db.close()


