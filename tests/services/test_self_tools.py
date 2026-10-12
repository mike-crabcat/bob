"""The `self` tool (services/self_tools.py) + KpiRepository."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from server.repositories.conversations import ConversationRepository
from server.services.self_tools import make_self_tools

DM = "agent:main:whatsapp:dm:61400000001"
GROUP = "agent:main:whatsapp:group:120363000000000002"
GROUP2 = "agent:main:whatsapp:group:120363000000000003"


def _utc(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S")


async def _msg(db, mid, conv, *, sender=None, provenance=None, role="user", ago=0.0):
    await db.execute(
        "INSERT INTO messages (id, conversation_id, role, content, sender_id, "
        "provenance, created_at) VALUES (?, ?, ?, 'x', ?, ?, ?)",
        (mid, conv, role, sender, provenance, _utc(ago)))


async def _call(db, cid, *, category="whatsapp_incoming", session=None, ago=0.0):
    await db.execute(
        "INSERT INTO llm_call_log (id, created_at, provider, model, call_category, "
        "session_key, system_prompt, user_message, response_text, status) "
        "VALUES (?, ?, 'openrouter', 'm', ?, ?, '', '', '', 'completed')",
        (cid, _utc(ago), category, session))


def _tool(ctx, *, trusted=True, session=GROUP):
    return make_self_tools(ctx, session_key=session, is_trusted=trusted)[0].handler


async def test_kpi_counts_only_human_inbound_and_real_llm_calls(ctx):
    db = ctx.db
    await _msg(db, "m1", DM, sender="c-mike")
    await _msg(db, "m2", GROUP, sender="c-mike")
    await _msg(db, "m3", GROUP, sender="c-ryan")
    await _msg(db, "m4", GROUP2, sender="c-ryan")
    # not human inbound: steer, reaction, routine, assistant, import, goal room
    await _msg(db, "x1", GROUP, provenance="steer")
    await _msg(db, "x2", GROUP, sender="c-ryan", provenance="wa_reaction")
    await _msg(db, "x3", DM, provenance="routine")
    await _msg(db, "x4", GROUP, role="assistant")
    await _msg(db, "x5", GROUP, sender="c-ryan", provenance="whatsapp_import")
    await _msg(db, "x6", "agent:goal-1:utility", sender="c-mike")
    await _call(db, "l1")
    await _call(db, "l2", category="attention_probe")
    await _call(db, "e1", category="eval")
    await _call(db, "e2", category="eval_judge")
    await _call(db, "e3", category="attention_probe", session="eval:case:x")

    out = json.loads(await _tool(ctx)(command="kpi --days 3 --json"))
    assert len(out["days"]) == 3
    today = out["days"][-1]
    assert today == {"day": today["day"], "active_users": 2, "active_groups": 2,
                     "active_dms": 1, "messages_received": 4, "llm_calls": 2}
    assert out["window_distinct_users"] == 2


async def test_kpi_zero_fills_and_renders_table(ctx):
    await _msg(ctx.db, "m1", DM, sender="c-mike", ago=48)
    text = await _tool(ctx)(command="kpi --days 5")
    lines = text.splitlines()
    assert lines[0].startswith("day")
    assert len([ln for ln in lines if ln[:4].isdigit()]) == 5
    assert "1 distinct users" in text and "Last row is today" in text


async def test_aliases_and_usage_errors(ctx):
    run = _tool(ctx)
    assert "kpi [--days N]" in await run(command="help")
    assert "kpi [--days N]" in await run(command="")
    assert len(json.loads(await run(command="self stats 7 --json"))["days"]) == 7
    assert len(json.loads(await run(command="kpi --days=2 -j"))["days"]) == 2
    for bad in ("kpi --weeks 2", "kpi --days 0", "kpi --days lots", "frobnicate"):
        out = await run(command=bad)
        assert out.startswith("error:") and "kpi [--days N]" in out, bad


async def test_kpi_gate_trusted_or_policy(ctx):
    await ctx.db.execute(
        "INSERT INTO conversations (id, kind, created_at, updated_at) "
        "VALUES (?, 'group', datetime('now'), datetime('now'))", (GROUP,))
    untrusted = _tool(ctx, trusted=False)
    assert "isn't enabled" in await untrusted(command="kpi")
    assert "kpi [--days N]" in await untrusted(command="help")   # help stays open
    await ConversationRepository(ctx.db).set_policy(GROUP, {"self_kpi": True})
    assert "isn't enabled" not in await untrusted(command="kpi --days 2")


async def test_kpi_out_writes_workspace_file(ctx, tmp_path):
    ctx.settings.harness.workspace_dir = tmp_path
    await _msg(ctx.db, "m1", DM, sender="c-mike")
    run = _tool(ctx)
    out = await run(command="kpi --days 31 --out routines/kpi.json")
    assert out.startswith("wrote routines/kpi.json: 31 days")
    data = json.loads((tmp_path / "routines" / "kpi.json").read_text())
    assert len(data["days"]) == 31 and data["days"][-1]["active_dms"] == 1
    for bad in ("kpi --out ../escape.json", "kpi --out /etc/kpi.json",
                "kpi --out self/bob/kpi.json", "kpi --out"):
        res = await run(command=bad)
        assert res.startswith("error:"), bad
    assert not (tmp_path.parent / "escape.json").exists()
