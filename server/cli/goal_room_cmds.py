"""Bob CLI goal-rooms subapp (docs/goal-rooms-plan.md rollout: adopt legacy
active goals into rooms; see room state from the ops side)."""

from __future__ import annotations

import asyncio

from server.cli._helpers import *  # noqa: F403,F405

app = typer.Typer(help="Goal rooms: a conversation per goal")


async def _status() -> None:
    from pathlib import Path

    from server.config import Settings
    from server.context import AppContext
    from server.database import Database
    from server.repositories.goals import GoalRepository
    from server.repositories.stimulus import StimulusRepository
    from server.services.goal_rooms import is_room_session, rooms_enabled

    settings = Settings.from_env()
    db = Database(settings.db_path or Path("bob.db"),
                  Path(__file__).parent.parent / "schemas")
    await db.connect()
    await db.apply_migrations()
    ctx = AppContext(settings=settings, db=db)
    try:
        typer.echo(f"enabled: {rooms_enabled(ctx)}")
        rows = await GoalRepository(db).list_recent(limit=100)
        roomed = [r for r in rows if is_room_session(r["conversation_id"])]
        typer.echo(f"room goals (recent): {len(roomed)}")
        for r in roomed[:20]:
            typer.echo(f"  [{r['status']}] {r['id'][:8]} ({r['kind']}) "
                       f"{r['objective'][:60]}")
        routes = await StimulusRepository(db).enabled_goal_room_routes()
        typer.echo(f"live room routes: {len(routes)}")
        legacy = [g for g in await GoalRepository(db).list_active(limit=200)
                  if not is_room_session(g["conversation_id"])]
        typer.echo(f"legacy active goals (reviser path): {len(legacy)}")
    finally:
        await db.close()


async def _adopt(goal_id: str) -> int:
    from pathlib import Path

    from server.config import Settings
    from server.context import AppContext
    from server.database import Database
    from server.services.goal_rooms import adopt_goal

    settings = Settings.from_env()
    db = Database(settings.db_path or Path("bob.db"),
                  Path(__file__).parent.parent / "schemas")
    await db.connect()
    await db.apply_migrations()
    ctx = AppContext(settings=settings, db=db)
    try:
        result = await adopt_goal(ctx, goal_id)
    finally:
        await db.close()
    if not result.get("ok"):
        typer.echo(f"adopt failed: {result.get('error')}")
        return 1
    typer.echo(f"adopted: goal {goal_id} -> room {result['room']}")
    return 0


@app.command("status")
def status() -> None:
    """Goal rooms: enabled state, room goals, open routes."""
    asyncio.run(_status())


@app.command("adopt")
def adopt(
    goal_id: str = typer.Argument(..., help="active goal id to give a room"),
) -> None:
    """Give an existing ACTIVE goal a room (charter, subscriptions, check-ins)."""
    raise typer.Exit(asyncio.run(_adopt(goal_id)))


async def _grant(goal_id: str, groups: list[str], until: str, revoke: bool) -> int:
    from pathlib import Path

    from server.config import Settings
    from server.context import AppContext
    from server.database import Database
    from server.repositories.contacts import ContactRepository
    from server.services.goal_broadcast import grant_broadcast, revoke_broadcast

    settings = Settings.from_env()
    db = Database(settings.db_path or Path("bob.db"),
                  Path(__file__).parent.parent / "schemas")
    await db.connect()
    ctx = AppContext(settings=settings, db=db)
    try:
        if revoke:
            ok = await revoke_broadcast(ctx, goal_id)
            typer.echo("revoked" if ok else "no such goal")
            return 0 if ok else 1
        owner = await ContactRepository(db).get_default()
        result = await grant_broadcast(
            ctx, goal_id, groups,
            granted_by=str(owner["id"]) if owner else "operator", until=until,
            note="operator CLI (bob goal-rooms grant-broadcast)")
    finally:
        await db.close()
    if not result.get("ok"):
        typer.echo(f"grant failed: {result.get('error')}")
        return 1
    typer.echo(f"granted until {result['expires_at']}:")
    for g in result["groups"]:
        typer.echo(f"  {g['name']} ({g['gid']})")
    return 0


@app.command("grant-broadcast")
def grant_broadcast_cmd(
    goal_id: str = typer.Argument(..., help="active goal id"),
    groups: list[str] = typer.Argument(..., help="group names or JIDs Bob is in"),
    until: str = typer.Option("", "--until", help="ISO instant the grant lapses "
                              "(default: goal deadline + 2h, else 48h)"),
) -> None:
    """Owner-approve a goal to post into these groups (goal_broadcast in its room)."""
    raise typer.Exit(asyncio.run(_grant(goal_id, groups, until, False)))


@app.command("revoke-broadcast")
def revoke_broadcast_cmd(
    goal_id: str = typer.Argument(..., help="goal id"),
) -> None:
    """Remove a goal's broadcast grant."""
    raise typer.Exit(asyncio.run(_grant(goal_id, [], "", True)))
