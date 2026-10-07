"""Eval worlds — throwaway, production-shaped environments (docs/evals.md).

A run builds ONE template (migrations + the fictional base cast + the
background memory population + the operator snapshot) and every trial
copies it: its own SQLite file, its own workspace (persona bundle, the
fixture user.md, skill docs), its own settings. Nothing a world does can
reach the live bob.db or ~/workspace, and nothing needs cleaning up — the
world directory is deleted when the trial ends.

Stimuli enter through the real production entry points:

- ``inbound()`` — a human WhatsApp message: WhatsAppBridgeService.
  _handle_incoming_message (sender gate, session derivation, participant
  upsert, storage, the full DispatchSpec, DispatchRunner — final-text
  delivery, echo/narration guards, history recording, backburner).
- ``wake()`` — anything machine-originated (bg results, task relays,
  room wakes, nudges): wake_service.wake_conversation, which routes by
  channel exactly as production does.
- ``steer()`` — steering.create_request (owner bypass → steer wake).
- ``extract()`` — MemoryService.run_silent_turn_extraction.
- ``tick()`` — one heartbeat-ish pump: due effects + due goal wakeups.

Observation comes from the intercept probe: every turn (any channel) is
recorded with its messages, tool calls and final text; deliveries,
spawns, bg jobs and calls are recorded at the boundary.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from server.context import AppContext
from server.database import Database
from server.evals import fixtures as fx
from server.evals.fixtures import Group, Person

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"

_WEB_SEARCH_SERVER = {
    "name": "zai-web-search", "is_global": True, "trusted_only": False,
    "tools": [{
        "name": "web_search_prime",
        "description": ("Search web information, returns results including "
                        "web page title, web page URL, web page summary, "
                        "website name, website icon, etc."),
        "input_schema": {"type": "object", "properties": {
            "search_query": {"type": "string",
                             "description": "Content to be searched"},
            "content_size": {"type": "string", "description": (
                "medium (default, 400-600 words) or high (2500 words)")},
            "location": {"type": "string", "description": "cn or us"},
            "search_recency_filter": {"type": "string", "description": (
                "oneDay, oneWeek, oneMonth, oneYear, noLimit")},
        }, "required": ["search_query"]}}],
}

_NO_RESULTS = json.dumps({"results": []})


# --------------------------------------------------------------------------
# observation records
# --------------------------------------------------------------------------

@dataclass
class Turn:
    session_key: str
    call_category: str
    dispatch_id: str | None
    messages: list            # the live list run_turn mutated
    tools: list[str]
    background: bool
    started: float = field(default_factory=time.monotonic)
    seconds: float = 0.0
    result: str | None = None
    error: BaseException | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)

    def record_tool(self, name: str, args: dict, output: Any, *,
                    guarded: bool = False, seconds: float = 0.0) -> None:
        text = getattr(output, "text", output)
        self.tool_events.append({
            "name": name, "args": args, "output": str(text)[:4000],
            "guarded": guarded, "seconds": round(seconds, 2)})

    def finish(self, *, result: str | None = None,
               error: BaseException | None = None) -> None:
        self.result, self.error = result, error
        self.seconds = time.monotonic() - self.started

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """Every call the model emitted (from the transcript — includes
        calls to unknown tools, which never reach a handler)."""
        out = []
        for m in self.messages:
            if isinstance(m, dict) and m.get("type") == "function_call":
                out.append({"name": m.get("name", ""),
                            "arguments": m.get("arguments", "")})
            elif isinstance(m, dict) and m.get("role") == "assistant" \
                    and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    out.append({"name": fn.get("name", ""),
                                "arguments": fn.get("arguments", "")})
        return out


# --------------------------------------------------------------------------
# the run template
# --------------------------------------------------------------------------

@dataclass
class Template:
    root: Path
    db_file: Path
    now: datetime
    snapshot: dict
    people: dict[str, Person]
    groups: dict[str, Group]
    mcp_servers: list[dict]
    base_settings: Any
    persona_hash: str

    @classmethod
    async def build(cls, base_settings: Any, *, root: Path | None = None,
                    embed: bool = True) -> "Template":
        root = Path(root or tempfile.mkdtemp(prefix="bob-eval-run-"))
        root.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).replace(microsecond=0)
        snapshot = fx.load_snapshot(Path(base_settings.data_dir))
        people = {p.first: copy.copy(p) for p in fx.CAST}
        groups = {g.name: copy.deepcopy(g) for g in fx.GROUPS}
        mcp_servers = snapshot.get("mcp_servers") or [_WEB_SEARCH_SERVER]
        tpl = cls(root=root, db_file=root / "template.db", now=now,
                  snapshot=snapshot, people=people, groups=groups,
                  mcp_servers=mcp_servers, base_settings=base_settings,
                  persona_hash=fx.fingerprint(snapshot))
        await tpl._populate(embed=embed)
        return tpl

    async def _populate(self, *, embed: bool = True) -> None:
        from server.repositories.contacts import ContactRepository
        from server.repositories.conversations import ConversationRepository
        from server.repositories.groups import GroupRepository
        from server.repositories.participants import ParticipantRepository
        from server.services.memory import MemoryService
        from server.services.memory.claim_service import write_claim
        from server.services.memory.eval_fixtures import seed_entity
        from server.services.memory.models import Claim

        ws = self.root / "template-ws"
        ws.mkdir(exist_ok=True)
        db = Database(db_path=self.db_file, schema_dir=SCHEMA_DIR, pool_size=1)
        await db.connect()
        await db.apply_migrations()
        try:
            settings = _settings_for(self.base_settings, self.root / "template-data", ws, self.db_file)
            ctx = AppContext(db=db, settings=settings)
            now_iso = self.now.isoformat()
            contacts = ContactRepository(db)
            convs = ConversationRepository(db)
            parts = ParticipantRepository(db)
            for p in self.people.values():
                p.contact_id = await contacts.create(
                    name=p.name, phone_number=f"+{p.phone}", email=p.email,
                    is_trusted=1 if p.trusted else 0)
                if p.owner:
                    await contacts.set_default(p.contact_id)
                # Inbound order: derive (ensure, phone address) → register.
                await convs.ensure(p.dm_key, address=f"+{p.phone}",
                                   endpoint_kind="dm")
                await convs.register_endpoint(
                    p.dm_key, endpoint_kind="dm", address=p.jid,
                    contact_id=p.contact_id)
                await parts.upsert(p.dm_key, f"+{p.phone}", display_name=p.name,
                                   contact_id=p.contact_id,
                                   is_trusted=p.trusted, now_iso=now_iso)
            grepo = GroupRepository(db)
            for g in self.groups.values():
                gid = await grepo.upsert_group(
                    g.jid, name=g.name, description="",
                    member_count=len(g.members), now_iso=now_iso)
                await convs.ensure(g.key, address=g.jid, endpoint_kind="group",
                                   title=g.name)
                await convs.register_endpoint(g.key, endpoint_kind="group",
                                              address=g.jid)
                if g.policy:
                    await convs.set_policy(g.key, g.policy)
                for first in g.members:
                    p = self.people[first]
                    await grepo.upsert_member(gid, p.contact_id,
                                              display_name=p.name,
                                              now_iso=now_iso)
                    await parts.upsert(g.key, f"+{p.phone}", display_name=p.name,
                                       contact_id=p.contact_id,
                                       is_trusted=p.trusted, now_iso=now_iso)
                await grepo.set_memory_entity(gid, g.slug)

            from server.services.memory.eval_fixtures import no_embedding
            mem = MemoryService(ctx)
            await mem.ensure_self_entity()
            seeded = set()
            for eid, etype, name, claims in fx.base_memory(self.now):
                await seed_entity(ctx, eid, etype, name, claims, embed=False)
                seeded.add(eid)
            for p in self.people.values():
                if p.slug not in seeded:
                    await seed_entity(ctx, p.slug, "person", p.name, [],
                                      embed=False)
                await write_claim(db, Claim(
                    id=f"claim-person-{p.slug}-contact_id",
                    claim_type_key="contact_id", subject_id=p.slug,
                    value=p.contact_id[:8], visibility="private"))
            for c in self.snapshot["self_claims"]:
                await write_claim(db, Claim(
                    id=f"claim-self-{abs(hash(c['value'])) % 10**12}",
                    claim_type_key=c["claim_type_key"], subject_id="self-bob",
                    value=c["value"]))
            with no_embedding():
                await mem.rebuild_fts()
            if embed:
                try:
                    await mem.rebuild_embeddings()
                except Exception:
                    pass  # no embedding key → FTS-only recall, as prod degrades
        finally:
            await db.close()

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _settings_for(base: Any, data_dir: Path, workspace: Path, db_file: Path) -> Any:
    s = copy.deepcopy(base)
    data_dir.mkdir(parents=True, exist_ok=True)
    s.data_dir = data_dir
    s.db_path = db_file
    s.log_dir = data_dir / "logs"
    s.harness.workspace_dir = workspace
    # Production keeps bridge media inside the workspace (read_image only
    # reads workspace paths): BOB_WHATSAPP_BRIDGE_MEDIA_DIR=~/workspace/whatsapp-media
    s.whatsapp_bridge.media_dir = workspace / "whatsapp-media"
    s.whatsapp_bridge.media_dir.mkdir(parents=True, exist_ok=True)
    return s


# --------------------------------------------------------------------------
# a world
# --------------------------------------------------------------------------

class World:
    def __init__(self, template: Template, root: Path, *,
                 pin_model: str | None = None, effort: str | None = None) -> None:
        self.template = template
        self.root = root
        self.now = template.now
        self.people = template.people
        self.groups = template.groups
        self.pin_model = pin_model
        self.effort = effort
        self.pin_background = False
        self.workspace = root / "workspace"
        self.ctx: AppContext | None = None
        self.turns: list[Turn] = []
        self.deliveries: list[dict[str, Any]] = []
        self.spawns: list[dict[str, Any]] = []
        self.bg_jobs: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.flags: dict[str, Any] = {}
        self.ha_states: dict[str, Any] = {}
        self.mcp_handlers: dict[tuple[str, str], Callable[..., Any]] = {}
        self._fail: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._msg_seq = itertools.count(1)
        self._token = None
        self.id = root.name
        self.api_calls: list[dict[str, Any]] = []
        self.api_down: dict[str, str] = {}
        self.real_subagents = False
        self.real_image_api = False   # gateway forwards image calls to OpenAI
        self.bg_timeout = 180.0
        # Attention: discipline cases dispatch immediately (a turn is what's
        # under test); capability scenarios can run the real coordinator —
        # windows, tier-2 probe, mid-turn re-arm — on world-local state.
        self.attention_real = False
        self.attention_windows: dict[str, Any] = {}
        self.attention_dispatching: set[str] = set()
        self.dispatch_locks: dict[str, asyncio.Lock] = {}
        self.overdue_last_wake: dict[str, float] = {}
        self.claude_runs: list[dict[str, Any]] = []
        self.claude_reply = ("Done — changes made in the workspace and "
                             "verified; summary in the session log.")

    # ---- lifecycle -------------------------------------------------------

    @classmethod
    async def open(cls, template: Template, **kwargs: Any) -> "World":
        root = Path(tempfile.mkdtemp(prefix="world-", dir=template.root))
        w = cls(template, root, **kwargs)
        await w._build()
        return w

    async def _build(self) -> None:
        from server.evals import intercept
        from server.repositories.mcp import McpServerRepository

        db_file = self.root / "world.db"
        shutil.copyfile(self.template.db_file, db_file)
        self._build_workspace()
        settings = _settings_for(self.template.base_settings,
                                 self.root / "data", self.workspace, db_file)
        db = Database(db_path=db_file, schema_dir=SCHEMA_DIR, pool_size=2)
        await db.connect()
        self.ctx = AppContext(db=db, settings=settings)
        self.ctx.whatsapp_bridge = intercept.make_fake_bridge(self.ctx)
        intercept.ensure_executors()

        servers = []
        repo = McpServerRepository(db)
        for s in self.template.mcp_servers:
            row = await repo.create(
                name=s["name"], transport="stdio", command="eval-fixture",
                is_global=bool(s.get("is_global", True)),
                trusted_only=bool(s.get("trusted_only", False)),
                created_by="eval-world")
            servers.append({**s, "id": row["id"]})
            for t in s["tools"]:
                self.mcp_handlers[(s["name"], t["name"])] = _unavailable(t["name"])
        self.ctx.mcp = intercept.make_fixture_mcp(self.ctx, servers)
        self.web({})

    def _build_workspace(self) -> None:
        ws = self.workspace
        (ws / "self").mkdir(parents=True)
        shutil.copytree(fx.REPO_ROOT / "self", ws / "self", dirs_exist_ok=True)
        (ws / "user.md").write_text(fx.USER_MD)
        src = fx.skill_sources(self.template.snapshot)
        for skill_dir in sorted(p for p in src.iterdir() if p.is_dir()):
            for name in ("SKILL.md", "skill.md"):
                if (skill_dir / name).is_file():
                    dst = ws / "skills" / skill_dir.name
                    dst.mkdir(parents=True, exist_ok=True)
                    (dst / name).write_text((skill_dir / name).read_text())
        for d in ("generated-images", "scratch", "docs"):
            (ws / d).mkdir(exist_ok=True)
        (self.root / "home").mkdir(exist_ok=True)

    async def close(self) -> None:
        from server.evals.intercept import release_world
        release_world(self)
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.ctx is not None:
            await self.ctx.db.close()
        shutil.rmtree(self.root, ignore_errors=True)

    @contextmanager
    def active(self):
        from server.evals.intercept import CURRENT_WORLD
        token = CURRENT_WORLD.set(self)
        try:
            yield self
        finally:
            CURRENT_WORLD.reset(token)

    @property
    def db(self) -> Database:
        assert self.ctx is not None
        return self.ctx.db

    # ---- addressing ------------------------------------------------------

    def person(self, first: str) -> Person:
        return self.people[first]

    def group(self, name: str) -> Group:
        return self.groups[name]

    @property
    def owner(self) -> Person:
        return next(p for p in self.people.values() if p.owner)

    def key(self, chat: "Person | Group | str") -> str:
        if isinstance(chat, Person):
            return chat.dm_key
        if isinstance(chat, Group):
            return chat.key
        if chat in self.people:
            return self.people[chat].dm_key
        if chat in self.groups:
            return self.groups[chat].key
        return chat

    def day(self, n: int, fmt: str = "%Y-%m-%d") -> str:
        """A date relative to the world's now, in Perth local time."""
        from zoneinfo import ZoneInfo
        return (self.now.astimezone(ZoneInfo("Australia/Perth"))
                + timedelta(days=n)).strftime(fmt)

    # ---- setup -----------------------------------------------------------

    def files(self, tree: dict[str, str | bytes]) -> None:
        for rel, content in tree.items():
            path = self.workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content)
                if content.startswith("#!"):
                    path.chmod(0o755)

    def skill(self, name: str, skill_md: str,
              files: dict[str, str | bytes] | None = None) -> None:
        for doc in ("skill.md", "SKILL.md"):   # replace a snapshot doc
            (self.workspace / "skills" / name / doc).unlink(missing_ok=True)
        self.files({f"skills/{name}/skill.md": skill_md})
        self.files({f"skills/{name}/{rel}": c for rel, c in (files or {}).items()})

    async def memory(self, entity_id: str, entity_type: str, name: str,
                     claims: list[tuple[str, str]]) -> None:
        from server.services.memory.eval_fixtures import seed_entity
        await seed_entity(self.ctx, entity_id, entity_type, name, claims)

    async def history(self, chat: "Person | Group | str",
                      rows: list[tuple], *,
                      ago: timedelta = timedelta(minutes=30),
                      gap: timedelta = timedelta(minutes=1),
                      provenance: dict[int, str] | None = None) -> list[str]:
        """Seed prior conversation. rows: (speaker first name | 'Bob', text)
        or (speaker, text, metadata) — e.g. {"image_path": …} for a photo.
        Backdated so the last row lands ``ago`` before now."""
        from server.repositories.history import HistoryRepository
        from server.services.session_service import SessionService
        key = self.key(chat)
        svc, hist = SessionService(self.ctx), HistoryRepository(self.db)
        start = self.now - ago - gap * max(len(rows) - 1, 0)
        ids = []
        for i, row in enumerate(rows):
            speaker, text = row[0], row[1]
            meta = row[2] if len(row) > 2 else None
            if speaker == "Bob":
                mid = await svc.add_message(key, "assistant", text,
                                            channel="whatsapp", synthetic=False,
                                            metadata=meta)
            elif speaker == "system":
                mid = await svc.add_message(
                    key, "user", text, channel="whatsapp", dispatched=1,
                    metadata=meta, provenance=(provenance or {}).get(i, "wake_nudge"))
            else:
                p = self.people[speaker]
                mid = await svc.add_message(
                    key, "user", text, channel="whatsapp",
                    sender_id=p.contact_id, dispatched=1, metadata=meta,
                    provenance=(provenance or {}).get(i))
            ts = (start + gap * i).strftime("%Y-%m-%d %H:%M:%S")
            await hist.set_created_at(mid, ts)
            ids.append(mid)
        return ids

    async def filler(self, chat: "Person | Group | str", n: int,
                     speakers: list[str], *,
                     ago: timedelta = timedelta(minutes=30)) -> None:
        lines = ["haha yeah", "who's bringing the esky", "on my way",
                 "did anyone see that", "classic", "ok sweet",
                 "running 10 late", "what a mark", "lol", "true",
                 "can't believe that", "next round's on me"]
        rows = [(speakers[i % len(speakers)], lines[i % len(lines)])
                for i in range(n)]
        await self.history(chat, rows, ago=ago, gap=timedelta(seconds=30))

    def web(self, results: dict[str, Any] | Callable[..., Any]) -> None:
        """Fixture web search: {substring: result} (first match wins) or a
        callable(search_query=…, **kw). Unmatched → empty results."""
        def handler(**kwargs: Any) -> Any:
            q = str(kwargs.get("search_query", "")).lower()
            if callable(results):
                return results(**kwargs)
            for needle, out in results.items():
                if needle.lower() in q:
                    return out
            return _NO_RESULTS
        for s in self.template.mcp_servers:
            for t in s["tools"]:
                if t["name"] == "web_search_prime":
                    self.mcp_handlers[(s["name"], t["name"])] = handler

    def outage(self, service: str, error: str) -> None:
        """Make a gateway service ('openai' | 'runware') answer 503."""
        self.api_down[service] = error

    def fail(self, kind: str, error: str) -> None:
        """Make deliveries of an effect kind fail (e.g. whatsapp_send_media)."""
        self._fail[kind] = error

    # ---- stimuli ---------------------------------------------------------

    async def inbound(self, sender: "Person | str", text: str, *,
                      group: "Group | str | None" = None,
                      media: dict | None = None,
                      mentions: list["Person | str"] | None = None) -> list[Turn]:
        p = sender if isinstance(sender, Person) else self.people[sender]
        g = (group if isinstance(group, Group) or group is None
             else self.groups[group])
        mark = len(self.turns)
        payload = {
            "chat_id": g.jid if g else p.jid,
            "chat_kind": "group" if g else "dm",
            "sender_jid": p.jid, "sender_name": p.name, "text": text,
            "whatsapp_message_id": f"EVAL{next(self._msg_seq):06d}",
            "mentioned_jids": [
                (m if isinstance(m, Person) else self.people[m]).jid
                for m in (mentions or [])],
        }
        if media:
            payload["media"] = media
        await self.ctx.whatsapp_bridge._handle_incoming_message(payload)
        await self.settle()
        return self.turns[mark:]

    async def wake(self, chat: "Person | Group | str", content: str, *,
                   provenance: str = "wake_nudge", category: str = "wakeup",
                   metadata: dict | None = None) -> list[Turn]:
        from server.services.wake_service import wake_conversation
        mark = len(self.turns)
        await wake_conversation(self.ctx, self.key(chat), content,
                                call_category=category, metadata=metadata,
                                provenance=provenance)
        await self.settle()
        return self.turns[mark:]

    async def steer(self, requester: "Person | str", target: "Person | Group | str",
                    instruction: str, *, origin: "Person | Group | str | None" = None
                    ) -> list[Turn]:
        from server.services import steering
        p = requester if isinstance(requester, Person) else self.people[requester]
        origin_key = self.key(origin) if origin else p.dm_key
        target_key = self.key(target)
        mark = len(self.turns)
        await steering.create_request(
            self.ctx, target_key=target_key,
            target_label=await steering.session_label(self.ctx, target_key),
            instruction=instruction, origin_session_key=origin_key,
            origin_label=await steering.session_label(self.ctx, origin_key),
            requester_contact_id=p.contact_id)
        await self.settle()
        return self.turns[mark:]

    async def extract(self, chat: "Person | Group | str", *,
                      hint: str | None = None) -> list[Turn]:
        from server.services.memory import MemoryService
        mark = len(self.turns)
        await MemoryService(self.ctx).run_silent_turn_extraction(
            self.key(chat), force=True, hint=hint, trigger="eval")
        await self.settle()
        return self.turns[mark:]

    async def email(self, sender_email: str, sender_name: str, subject: str,
                    text: str, *, thread: str | None = None) -> list[Turn]:
        """An inbound email through EmailPollingService's real ingress."""
        import uuid as _uuid
        from server.evals.intercept import FakeAgentMail
        from server.services.email_polling_service import EmailPollingService
        from server.services.email_store import EmailStore
        store = EmailStore(self.db)
        inbox = await store.inbox_by_agentmail_id("bob@eval.example")
        if inbox is None:
            await store.insert_inbox(
                inbox_id=str(_uuid.uuid4()), agentmail_inbox_id="bob@eval.example",
                display_name="Bob", email_address="bob@eval.example",
                metadata_json="{}", now_iso=self.now.isoformat())
            inbox = await store.inbox_by_agentmail_id("bob@eval.example")
        n = next(self._msg_seq)
        msg = {"message_id": f"<eval-{n}@mail.example>",
               "thread_id": thread or f"thread-eval-{n}",
               "from": f"{sender_name} <{sender_email}>",
               "to": ["bob@eval.example"], "subject": subject, "text": text,
               "extracted_text": text, "preview": text[:120],
               "labels": ["received", "unread"],
               "timestamp": self.now.isoformat(), "attachments": []}
        mark = len(self.turns)
        svc = EmailPollingService(self.ctx, agentmail_client=FakeAgentMail())
        await svc.process_incoming_message(inbox, msg)
        await self.settle()
        return self.turns[mark:]

    async def goals(self) -> list[dict[str, Any]]:
        """Every goal in the world (worlds start with none)."""
        from server.repositories.goals import GoalRepository
        return await GoalRepository(self.db).list_recent(limit=500)

    async def claims(self, subject_id: str | None = None) -> list[Any]:
        from server.services.memory.claim_service import get_all_claims
        out = await get_all_claims(self.db)
        return [c for c in out if subject_id is None or c.subject_id == subject_id]

    async def tick(self, *, advance: timedelta | None = None) -> list[Turn]:
        """One pump round: due effects, then due goal wakeups. ``advance``
        pulls forward every wakeup scheduled within that window first, so
        schedules come due without faking the clock."""
        from server.repositories.wakeups import WakeupRepository
        from server.services.effects import pump_due_effects
        from server.services.goal_service import pump_due_wakeups
        mark = len(self.turns)
        if advance is not None:
            await WakeupRepository(self.db).pull_forward(advance.total_seconds())
        await pump_due_effects(self.ctx)
        await pump_due_wakeups(self.ctx)
        await self.settle()
        return self.turns[mark:]

    async def settle(self, timeout: float = 900.0) -> None:
        """Wait until every task this world started has finished."""
        deadline = time.monotonic() + timeout
        while True:
            pending = [t for t in self._tasks if not t.done()
                       and t is not asyncio.current_task()]
            armed = any(getattr(win, "timer", None) is not None
                        for win in self.attention_windows.values())
            if not pending and not armed and not self.attention_dispatching:
                return
            if not pending:
                await asyncio.sleep(0.25)       # an attention window is open
                if time.monotonic() > deadline:
                    raise TimeoutError("world did not settle: attention window open")
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"world did not settle: {len(pending)} task(s)")
            await asyncio.wait(pending, timeout=min(left, 5.0))

    # ---- hooks called by intercept ---------------------------------------

    def track_task(self, task: asyncio.Task) -> None:
        self._tasks.add(task)

    def begin_turn(self, **kw: Any) -> Turn:
        t = Turn(**kw)
        self.turns.append(t)
        return t

    def record_delivery(self, kind: str, payload: dict[str, Any]) -> str:
        if kind in self._fail:
            raise RuntimeError(self._fail[kind])
        rec = {"kind": kind, **payload, "seq": len(self.deliveries) + 1}
        self.deliveries.append(rec)
        return f"eval-{kind}-{rec['seq']}"

    async def record_spawn(self, payload: dict[str, Any]) -> str:
        self.spawns.append(dict(payload))
        return payload["subagent_id"]

    def record_claude_run(self, *a: Any, **kw: Any) -> dict[str, Any]:
        prompt = kw.get("prompt") or (a[0] if a else "")
        self.claude_runs.append({"prompt": str(prompt)[:4000],
                                 "resume": kw.get("session_id")})
        return {"result": self.claude_reply,
                "session_id": f"eval-claude-{len(self.claude_runs)}",
                "cost_usd": 0.0}

    def record_bg_start(self, name: str, command: str, log: Path) -> int:
        """A bg job in a world really runs — in the workspace, scrubbed env,
        host guard applied — and its exit goes through production's own
        poll_bg_jobs → exit wake. Capped at bg_timeout seconds."""
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("")
        self.bg_jobs[name] = {"command": command, "alive": True,
                              "exit_code": None, "proc": None}
        self.track_task(asyncio.ensure_future(self._run_bg(name, command, log)))
        return 4_000_000 + len(self.bg_jobs)

    async def _run_bg(self, name: str, command: str, log: Path) -> None:
        from server.evals.intercept import _BASH_HOST_GUARD
        from server.services import process_tools, workspace_tools
        job = self.bg_jobs[name]
        ws = str(self.workspace)
        cmd = (command.replace("/home/bob/workspace", ws)
               .replace("~/workspace", ws).replace("$HOME/workspace", ws))
        if _BASH_HOST_GUARD.search(cmd):
            log.write_text("BLOCKED: path outside the workspace\n")
            code = 126
        else:
            env = workspace_tools.build_skill_env(
                workspace_dir=ws,
                venv_dir=str(self.ctx.settings.harness.venv_dir.expanduser()))
            with log.open("ab") as out:
                proc = await asyncio.create_subprocess_exec(
                    "bash", "-c", cmd, cwd=ws, env=env, stdout=out,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                job["proc"] = proc
                try:
                    code = await asyncio.wait_for(proc.wait(), timeout=self.bg_timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    code = 137
        job.update(alive=False, exit_code=code)
        await process_tools.poll_bg_jobs(self.ctx)

    def record_bg_stop(self, name: str) -> None:
        job = self.bg_jobs.get(name)
        if job and job.get("proc") is not None and job["alive"]:
            try:
                job["proc"].kill()
            except ProcessLookupError:
                pass
        if job:
            job["alive"] = False

    def bg_alive(self, name: str) -> bool:
        return bool(self.bg_jobs.get(name, {}).get("alive"))

    def bg_exit(self, name: str) -> int | None:
        return self.bg_jobs.get(name, {}).get("exit_code")

    def record_call(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        return {"call_sid": f"CAeval{len(self.calls):04d}", "status": "queued"}

    # ---- observation -----------------------------------------------------

    @property
    def main_turns(self) -> list[Turn]:
        return [t for t in self.turns if not t.background]

    def turns_in(self, chat: "Person | Group | str") -> list[Turn]:
        key = self.key(chat)
        return [t for t in self.main_turns if t.session_key == key]

    def tool_calls(self, *, include_background: bool = False) -> list[dict]:
        turns = self.turns if include_background else self.main_turns
        return [c for t in turns for c in t.tool_calls]

    def called(self, *names: str) -> bool:
        return any(c["name"] in names for c in self.tool_calls())

    def sent_to(self, chat: "Person | Group | str") -> list[str]:
        """Texts/captions delivered to a chat."""
        if isinstance(chat, str) and chat not in self.people and chat not in self.groups:
            jid = chat
        else:
            target = (chat if isinstance(chat, (Person, Group))
                      else self.people.get(chat) or self.groups[chat])
            jid = target.jid
        return [d.get("text") or d.get("caption") or f"[media {d.get('file_path')}]"
                for d in self.deliveries
                if d.get("chat_id") == jid and d["kind"].startswith("whatsapp_send")]

    def reply(self, chat: "Person | Group | str | None" = None) -> str:
        """The final text of the last main turn (in ``chat`` when given)."""
        turns = self.turns_in(chat) if chat is not None else self.main_turns
        for t in reversed(turns):
            if t.result is not None:
                return t.result
        return ""

    def flag(self, key: str, value: Any) -> None:
        self.flags[key] = value

    def chat_label(self, jid: str) -> str:
        for p in self.people.values():
            if p.jid == jid:
                return f"{p.name} (DM)"
        for g in self.groups.values():
            if g.jid == jid:
                return f"{g.name} (group)"
        return jid


def _unavailable(tool: str) -> Callable[..., str]:
    def handler(**_: Any) -> str:
        return f"Error: {tool} is unavailable right now"
    return handler
