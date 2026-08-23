"""Bob CLI replay subapp — production-episode redaction tooling (Bob3 Phase VII).

``bob replay export-episode`` samples a slice of a production conversation
from the event log and emits a REDACTED episode fixture in the replay
harness format (``tests/fixtures/episodes/``). The raw production log is
not the eval dataset: phones are remapped to a fake range, sender names
become "Person A/B/…", and phone numbers / emails / URLs inside message
text are masked. The operator still reviews the JSON before committing —
mechanical redaction can't judge semantic sensitivity.
"""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405


app = typer.Typer(help="Replay corpus tooling (episode export + redaction)")

_PHONE_RE = None
_EMAIL_RE = None
_URL_RE = None


def _redactors():
    global _PHONE_RE, _EMAIL_RE, _URL_RE
    import re
    if _PHONE_RE is None:
        _PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")
        _EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
        _URL_RE = re.compile(r"https?://\S+")
    return _PHONE_RE, _EMAIL_RE, _URL_RE


def _redact_text(text: str) -> str:
    phone_re, email_re, url_re = _redactors()
    text = url_re.sub("<url>", text or "")
    text = email_re.sub("<email>", text)
    return phone_re.sub("<phone>", text)


@app.command("export-episode")
def export_episode(
    session_key: Annotated[str, typer.Argument(help="Conversation/session key to sample")],
    name: Annotated[str, typer.Option("--name", "-n", help="Episode name (also the output filename)")],
    since: Annotated[Optional[str], typer.Option("--since", help="ISO lower bound on occurred_at")] = None,
    until: Annotated[Optional[str], typer.Option("--until", help="ISO upper bound on occurred_at")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max messages to include")] = 20,
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Output path (default tests/fixtures/episodes/<name>.json)")] = None,
) -> None:
    """Export a redacted production episode as a replay fixture."""
    import asyncio
    asyncio.run(_export_episode(session_key, name, since, until, limit, out))


async def _export_episode(
    session_key: str, name: str, since: str | None, until: str | None,
    limit: int, out: Path | None,
) -> None:
    import json

    from bob_server.config import Settings
    from bob_server.database import Database

    settings = Settings.from_env()
    db = Database(db_path=settings.db_path, schema_dir=None, pool_size=1)
    await db.connect()
    try:
        clauses = ["event_type = 'message.received'", "conversation_id = ?"]
        params: list[object] = [session_key]
        if since:
            clauses.append("occurred_at >= ?")
            params.append(since)
        if until:
            clauses.append("occurred_at <= ?")
            params.append(until)
        events = await db.fetch_all(
            f"SELECT * FROM event_log WHERE {' AND '.join(clauses)} "
            f"ORDER BY id LIMIT ?", (*params, limit))
        if not events:
            typer.echo("No message.received events in that range.", err=True)
            raise typer.Exit(1)

        phone_map: dict[str, str] = {}
        name_map: dict[str, str] = {}

        def fake_phone(real: str) -> str:
            if real not in phone_map:
                phone_map[real] = f"+6140000000{10 + len(phone_map)}"
            return phone_map[real]

        def fake_name(real: str) -> str:
            if real not in name_map:
                name_map[real] = f"Person {chr(ord('A') + len(name_map))}"
            return name_map[real]

        messages = []
        window = (events[0]["occurred_at"], events[-1]["occurred_at"])
        for i, ev in enumerate(events, 1):
            payload = json.loads(ev["payload_json"] or "{}")
            sm = await db.fetch_one(
                "SELECT content FROM messages WHERE id = ?",
                (payload.get("session_message_id"),))
            sender = payload.get("sender_name") or ""
            text = _redact_text(sm["content"] if sm else "")
            if sender:
                fake_name(sender)  # register mapping for the report
            digits = ""
            import re as _re
            m = _re.search(r":(?:dm|group):(\d+)", ev["binding_key"] or "")
            digits = m.group(1) if m else ""
            messages.append({
                "kind": payload.get("chat_kind") or "dm",
                "phone": fake_phone(digits or sender or "unknown"),
                "text": text,
                "msg_id": f"ep-{name}-{i}",
            })

        # Second pass: mask known sender names inside message text ("Mike
        # said…") using the same alias map. Unknown in-text names still
        # require operator review.
        if name_map:
            import re as _re2
            name_pat = _re2.compile(
                "|".join(_re2.escape(n) for n in sorted(name_map, key=len, reverse=True)))
            for m_ in messages:
                m_["text"] = name_pat.sub(lambda mo: name_map[mo.group(0)], m_["text"])

        # Observed outcome for the expect skeleton: turns + send effects in
        # the episode window for this conversation.
        turns = await db.fetch_one(
            """SELECT COUNT(*) AS n FROM turns
               WHERE conversation_id = ? AND created_at BETWEEN ? AND ?""",
            (session_key, window[0], window[1] + "\uffff"))
        sends = await db.fetch_one(
            """SELECT COUNT(*) AS n FROM effects e JOIN turns t ON t.id = e.turn_id
               WHERE t.conversation_id = ? AND e.kind LIKE '%send%'
                 AND e.created_at >= ?""",
            (session_key, window[0]))
        reply = await db.fetch_one(
            """SELECT content FROM messages
               WHERE conversation_id = COALESCE(
                   (SELECT conversation_id FROM bindings WHERE session_key = ?), ?)
                 AND role = 'assistant' AND created_at >= ?
               ORDER BY created_at LIMIT 1""",
            (session_key, session_key, window[0]))

        episode = {
            "name": name,
            "description": f"REDACTED production episode from {session_key} "
                           f"({window[0]} .. {window[1]}) — REVIEW BEFORE COMMIT.",
            "llm_reply": _redact_text(reply["content"]) if reply else "",
            "messages": messages,
            "expect": {
                "turns": turns["n"] if turns else 0,
                "send_effects": sends["n"] if sends else 0,
            },
        }

        if out is None:
            out = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "episodes" / f"{name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(episode, indent=2) + "\n")
        typer.echo(f"Wrote {out}")
        typer.echo(f"Redaction map: phones={phone_map} names={name_map}")
        typer.echo("REVIEW the text fields before committing — mechanical "
                   "redaction masks phones/emails/URLs only.")
    finally:
        await db.close()


@app.command("export-probe-candidates")
def export_probe_candidates(
    limit: Annotated[int, typer.Option("--limit", help="Shadow rows to sample")] = 30,
    out: Annotated[Optional[Path], typer.Option("--out", "-o")] = None,
) -> None:
    """Dump recent attention_shadow decisions with redacted context as
    candidate golden cases (operator curates labels into probe_golden.json)."""
    import asyncio
    asyncio.run(_export_probe_candidates(limit, out))


async def _export_probe_candidates(limit: int, out: Path | None) -> None:
    import json

    from bob_server.config import Settings
    from bob_server.database import Database

    settings = Settings.from_env()
    db = Database(db_path=settings.db_path, schema_dir=None, pool_size=1)
    await db.connect()
    try:
        rows = await db.fetch_all(
            """SELECT * FROM attention_shadow
               WHERE chat_kind = 'group' AND addressed = 0
               ORDER BY id DESC LIMIT ?""", (limit,))
        cases = []
        for r in rows:
            msgs = await db.fetch_all(
                """SELECT role, content FROM messages
                   WHERE conversation_id = COALESCE(
                       (SELECT conversation_id FROM bindings WHERE session_key = ?), ?)
                     AND created_at <= ?
                   ORDER BY created_at DESC LIMIT 6""",
                (r["session_key"], r["session_key"], r["created_at"]))
            lines = []
            for m in reversed(msgs):
                who = "Bot" if m["role"] == "assistant" else "User"
                lines.append(f"{who}: {_redact_text(m['content'] or '')}")
            cases.append({
                "name": f"shadow-{r['id']}",
                "context": "Recent group messages:\n" + "\n".join(lines),
                "label": r["decision"],
                "provisional": True,
            })
        payload = {
            "description": "CANDIDATE probe cases from attention_shadow — labels are "
                           "the shadow's own decisions, NOT golden truth. Review, "
                           "correct, de-provisionalise, then merge into probe_golden.json.",
            "cases": cases,
        }
        if out is None:
            out = Path("probe_candidates.json")
        out.write_text(json.dumps(payload, indent=2) + "\n")
        typer.echo(f"Wrote {len(cases)} candidate case(s) to {out} — review labels before use.")
    finally:
        await db.close()


@app.command("probe-matrix")
def probe_matrix(
    golden: Annotated[Optional[Path], typer.Option("--golden", "-g", help="Golden labels JSON")] = None,
    model: Annotated[str, typer.Option("--model", "-m")] = "gpt-5.6-luna",
) -> None:
    """Score the Tier 2 probe against golden labels: confusion matrix +
    accuracy. Runs live LLM calls (needs API credits). Use before deploying
    any probe prompt change (Phase VII exit criterion)."""
    import asyncio
    asyncio.run(_probe_matrix(golden, model))


async def _probe_matrix(golden: Path | None, model: str) -> None:
    import json

    from bob_server.config import Settings
    from bob_server.context import AppContext
    from bob_server.database import Database
    from bob_server.services.attention.tier2 import probe_decide

    if golden is None:
        golden = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "probe_golden.json"
    data = json.loads(golden.read_text())
    cases = data["cases"]
    bot_name = data.get("bot_name", "Bob")

    settings = Settings.from_env()
    db = Database(db_path=settings.db_path, schema_dir=None, pool_size=1)
    await db.connect()
    ctx = AppContext(db=db, settings=settings)
    labels = ("ACT", "WAIT", "STAND_DOWN")
    matrix = {g: {p: 0 for p in labels} for g in labels}
    failures = []
    try:
        for case in cases:
            predicted = await probe_decide(
                ctx, case["context"], bot_name=bot_name, model=model,
                session_key=f"eval:{case['name']}")
            gold = case["label"].upper()
            matrix[gold][predicted] += 1
            mark = "ok " if predicted == gold else "MISS"
            if predicted != gold:
                failures.append((case["name"], gold, predicted))
            typer.echo(f"  {mark} {case['name']:<32} gold={gold:<11} got={predicted}")
    finally:
        await db.close()

    total = sum(sum(row.values()) for row in matrix.values())
    correct = sum(matrix[l][l] for l in labels)
    typer.echo("\nConfusion matrix (rows=gold, cols=predicted):")
    typer.echo(f"{'':<12}" + "".join(f"{p:<12}" for p in labels))
    for g in labels:
        typer.echo(f"{g:<12}" + "".join(f"{matrix[g][p]:<12}" for p in labels))
    typer.echo(f"\nAccuracy: {correct}/{total} = {correct / total:.0%}" if total else "No cases.")
    if failures:
        typer.echo("Misses: " + ", ".join(f"{n} ({g}->{p})" for n, g, p in failures))
