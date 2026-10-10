"""``bob eval snapshot`` — capture production context for eval worlds.

Read-only, operator-invoked, out of the repo (~/data/evals/snapshot/):

- self_claims.json — self-bob's brief-whitelisted claims (the self-brief
  rides every production prompt; a world without it is a different Bob);
- skills/<name>/SKILL.md — the live skills index (docs only, no scripts:
  a world's skill scripts are planted per case);
- mcp_servers.json — global MCP servers' tool definitions, listed through
  the production refresh path (filters + caps applied).

Runs never read live state: they read this snapshot, and its content is
part of every run's persona_hash, so a re-snapshot shows up as a
comparability break in ``bob eval compare``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.evals.fixtures import snapshot_dir


async def take_snapshot(settings: Any, *, include_mcp: bool = True) -> str:
    from server.repositories.mcp import export_rows_readonly
    from server.services.memory.eval_fixtures import export_self_claims

    out = snapshot_dir(Path(settings.data_dir))
    tmp = out.with_name("snapshot.new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    notes = []

    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        claims = export_self_claims(conn)
        rows = export_rows_readonly(conn) if include_mcp else []
    finally:
        conn.close()
    (tmp / "self_claims.json").write_text(json.dumps(claims, indent=1))
    notes.append(f"{len(claims)} self claim(s)")

    ws_skills = Path(settings.harness.workspace_dir).expanduser() / "skills"
    n = 0
    for d in sorted(p for p in ws_skills.iterdir() if p.is_dir()) if ws_skills.is_dir() else []:
        for name in ("SKILL.md", "skill.md"):
            if (d / name).is_file():
                (tmp / "skills" / d.name).mkdir(parents=True, exist_ok=True)
                shutil.copyfile(d / name, tmp / "skills" / d.name / name)
                n += 1
                break
    notes.append(f"{n} skill doc(s)")

    if include_mcp:
        servers = await _mcp_tools(settings, [r for r in rows if r["is_global"]])
        (tmp / "mcp_servers.json").write_text(json.dumps(servers, indent=1))
        notes.append(", ".join(f"{s['name']}: {len(s['tools'])} tool(s)"
                               + (f" ({s['error']})" if s.get("error") else "")
                               for s in servers) or "no global MCP servers")
        skipped = [r["name"] for r in rows if not r["is_global"]]
        if skipped:
            notes.append(f"per-conversation servers not captured: {', '.join(skipped)}")

    (tmp / "SNAPSHOT.md").write_text(
        f"taken {datetime.now(UTC).isoformat()}\n" + "\n".join(notes) + "\n")
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    return f"snapshot → {out}\n  " + "\n  ".join(notes)


async def _mcp_tools(settings: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from server.context import AppContext
    from server.services.mcp_service import McpManager

    mgr = McpManager(AppContext(db=None, settings=settings))  # type: ignore[arg-type]
    out = []
    for row in rows:
        cache = await mgr.refresh_server(row)
        out.append({"name": row["name"], "is_global": bool(row["is_global"]),
                    "trusted_only": bool(row["trusted_only"]),
                    "error": cache.error,
                    "tools": [{"name": t["name"],
                               "description": t.get("description") or "",
                               "input_schema": t.get("input_schema") or {}}
                              for t in cache.tools]})
    return out
