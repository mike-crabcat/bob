"""The benchmark scenario runner (bob-events-plan.md §4.3).

"Bob, organise a team lunch for the AI-doom group" as a deterministic,
persona-scripted rehearsal: template kickoff → availability fan-out →
replies across channels (including the wrong-channel cases) → extraction →
routing → reviser folds → quorum decision → announcement → booking call
result → event memory + reminders → merch approval + skill-placed
POD order → (perturbation) cancellation loop.

Scope notes (deliberate): the WhatsApp inbound gate and attention windows
are exercised by their own suites — this harness injects persona messages
at the durable-stimulus level (SessionService.add_message, exactly what
the inbound pipeline stores) and drives everything downstream for real.
The voice leg is scripted at the call-result layer (real settle → roll-up
→ wake → event-memory chain); the audio-level text-mode callee (with the
§3.2 echo variant) and the one manual human-voiced run remain live-rehearsal
checklist items.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from server.context import AppContext

from .personas import FakeBridge, PersonaDriver, default_personas

log = logging.getLogger(__name__)

GROUP_KEY = "agent:main:whatsapp:group:doom"
WORK_KEY = "conv-mike-dm"          # internal channel → generic wake dispatch

_NAMES = ("alice", "bruno", "carol", "dan", "eve", "frank", "gina", "hank")
PERTURBATION_CHANNELS: dict[str, dict[str, str]] = {
    # persona → where their availability reply lands ("group" = wrong channel)
    "mixed": {"alice": "dm", "bruno": "dm", "carol": "dm", "dan": "dm",
              "eve": "group", "frank": "group", "gina": "dm", "hank": "dm"},
    "all_group": {n: "group" for n in _NAMES},
    "late_cancel": {"alice": "dm", "bruno": "group", "carol": "dm", "dan": "dm",
                    "eve": "group", "frank": "dm", "gina": "dm", "hank": "group"},
    "wrong_slug": {"alice": "dm", "bruno": "group", "carol": "group", "dan": "dm",
                   "eve": "group", "frank": "dm", "gina": "group", "hank": "dm"},
}


async def _wait_for(cond: Callable[[], Awaitable[bool]], timeout: float = 10.0,
                    what: str = "condition") -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await cond():
            return
        await asyncio.sleep(0.01)
    import asyncio as _a
    import os as _os
    if _os.getenv("REH_DEBUG"):
        for t in _a.all_tasks():
            if t is not _a.current_task():
                print("---- STUCK TASK ----", flush=True)
                t.print_stack(limit=14)
    raise AssertionError(f"rehearsal timed out waiting for: {what}")


def _noop():
    async def _f(*a, **k):
        return None
    return _f


@dataclass
class RehearsalScore:
    perturbation: str
    incidents: list[str] = field(default_factory=list)
    unnecessary_wakes: int = 0
    interventions: int = 0
    orders: int = 0
    group_replies: int = 0
    dm_replies: int = 0
    reminder_wakeups_fired: int = 0
    reviser_runs: int = 0

    @property
    def passed(self) -> bool:
        return (not self.incidents and self.unnecessary_wakes == 0
                and self.orders == 1 and self.interventions == 0)


class RehearsalScenario:
    def __init__(self, ctx: AppContext, monkeypatch, tmp_path: Path, *,
                 perturbation: str = "mixed"):
        self.ctx = ctx
        self.db = ctx.db
        self.monkeypatch = monkeypatch
        self.perturbation = perturbation
        self.tmp_path = tmp_path
        self.personas = default_personas(8)
        self.bridge = FakeBridge()
        self.driver = PersonaDriver(
            self.personas, GROUP_KEY, PERTURBATION_CHANNELS[perturbation])
        self.driver.bind(self.bridge)
        self.actors: Any = None
        self.bb: dict[str, Any] = {}
        self.dirty: set[str] = set()
        self.score = RehearsalScore(perturbation=perturbation)
        self._saved_send = None

    # ------------------------------------------------------------------
    async def setup(self) -> None:
        from tests.rehearsal.pod_stub import make_pod_stub
        from tests.rehearsal.scripted import ScriptedActors

        for env in ("BOB_CLAIM_ROUTER_DISABLED", "BOB_GOAL_STATE_SHADOW",
                    "BOB_GOAL_REVIEW_DISABLED"):
            self.monkeypatch.delenv(env, raising=False)

        # The POD stub stands in for the vendor Bob's printful skill talks
        # to (the skill itself lives in the workspace, outside the repo).
        self.pod_stub = make_pod_stub()
        self.pod_state = self.pod_stub.state.pod_state

        from server.services import effects as effects_svc
        # Save ONLY the key we override: a full-registry snapshot captured
        # before lazy imports register their executors would restore an
        # empty registry and break every later test in the process.
        self._saved_send = effects_svc._EXECUTORS.get("whatsapp_send")

        async def _exec_send(ctx, payload):
            return await self.bridge.send_message(
                payload["chat_id"], payload["text"],
                reply_to=payload.get("reply_to"))
        effects_svc.register_executor("whatsapp_send", _exec_send)

        self.actors = ScriptedActors(
            self.db, roster=[f"person-{p.name}" for p in self.personas],
            wrong_slug=(self.perturbation == "wrong_slug"),
            blackboard=self.bb)
        self.actors.install(self.monkeypatch)

        await self._seed_world()

    async def _seed_world(self) -> None:
        from server.repositories.conversations import ConversationRepository

        conv = ConversationRepository(self.db)

        async def contact(name: str, digits: str) -> str:
            cid = f"contact-{name}"
            await self.db.execute(
                "INSERT INTO contacts (id, name, phone_number, is_trusted, "
                "allow_inbound_dm, created_at, updated_at) VALUES "
                "(?, ?, ?, 1, 1, datetime('now'), datetime('now'))",
                (cid, name, f"+{digits}"))
            return cid

        self.mike_contact = await contact("mike", "61400000001")
        for p in self.personas:
            p.contact_id = await contact(p.name, p.phone_digits)

        await conv.register_endpoint(GROUP_KEY, endpoint_kind="group")
        await conv.ensure(WORK_KEY)   # the internal working conversation row
        for p in self.personas:
            await conv.register_endpoint(
                p.dm_session, endpoint_kind="dm", contact_id=p.contact_id)
            for conv_id in (GROUP_KEY, p.dm_session):
                await self.db.execute(
                    "INSERT OR REPLACE INTO participants (conversation_id, "
                    "identifier, display_name, contact_id, is_trusted, "
                    "last_active_at) VALUES (?, ?, ?, ?, 1, datetime('now'))",
                    (conv_id, p.phone_digits, p.name, p.contact_id))
        for conv_id in (GROUP_KEY, WORK_KEY):
            await self.db.execute(
                "INSERT OR REPLACE INTO participants (conversation_id, "
                "identifier, display_name, contact_id, is_trusted, "
                "last_active_at) VALUES (?, ?, 'mike', ?, 1, datetime('now'))",
                (conv_id, "61400000001", self.mike_contact))

        await conv.set_policy(GROUP_KEY, {"group_outbound_enabled": True})

    # ------------------------------------------------------------------
    async def kickoff(self) -> None:
        from server.services.goal_tools import make_goal_tools
        from server.services.whatsapp_outreach_tools import (
            make_whatsapp_outreach_tools,
        )

        tools = {t.name: t.handler for t in make_goal_tools(self.ctx, WORK_KEY)}
        decide_by = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        out = json.loads(await tools["instantiate_goal_template"](
            template="team-event",
            params_json=json.dumps({
                "event_name": "Team Lunch", "group_name": "AI Doom",
                "group_session_key": GROUP_KEY, "decide_by": decide_by})))
        assert out["ok"], out
        self.bb.update({"root": out["root_goal_id"], **out["children"],
                        "cart": {"recipient": {"name": "Mike",
                                               "address1": "1 Main St"},
                                 "items": [{"variant_id": 4012, "quantity": 8,
                                            "name": "team tee",
                                            "retail_price": 20.0}]}})

        outreach = {t.name: t.handler for t in make_whatsapp_outreach_tools(
            self.ctx, self.bridge, WORK_KEY)}
        for p in self.personas:
            r = json.loads(await outreach["send_whatsapp_to_contact"](
                contact_id=p.contact_id,
                message="Hey! Which day works for the team lunch — "
                        "Thursday or Friday?",
                objective=f"Get {p.name}'s availability and t-shirt size",
                parent_goal_id=self.bb["negotiate"]))
            assert r["ok"], r
        assert len(self.bridge.outbox) == 8

    # ------------------------------------------------------------------
    async def inject(self, persona, channel: str, text: str) -> None:
        from server.services.session_service import SessionService

        session = persona.dm_session if channel == "dm" else GROUP_KEY
        await SessionService(self.ctx).add_message(
            session, "user", text, channel="whatsapp",
            sender_id=persona.contact_id, dispatched=1)
        self.dirty.add(session)
        if channel == "group":
            self.score.group_replies += 1
        else:
            self.score.dm_replies += 1

    async def settle_ticks(self) -> None:
        """One pump round: extraction for dirty sessions (router runs inline
        in the extraction post-loop), outreach completion for answered DMs,
        then effects and wakeups."""
        from server.services.effects import pump_due_effects
        from server.services.goal_service import pump_due_wakeups
        from server.services.memory import MemoryService

        dirty, self.dirty = self.dirty, set()
        for session in dirty:
            await MemoryService(self.ctx).run_silent_turn_extraction(
                session, force=True)
            if ":dm:" in session:
                await self._finish_outreach(session)
        await pump_due_effects(self.ctx)
        await pump_due_wakeups(self.ctx)
        # Determinism: a tick ends only when every wake dispatch it spawned
        # has fully run (its main-model turn included) — no cross-tick races.
        from server.services import wake_service as ws
        pending = set(ws._pending_dispatches)
        while pending:
            await asyncio.wait(pending, timeout=10)
            pending = set(ws._pending_dispatches)
        await asyncio.sleep(0)

    async def _finish_outreach(self, dm_session: str) -> None:
        from server.repositories.goals import GoalRepository
        from server.services.whatsapp_outreach_tools import (
            make_outreach_reply_tools,
        )

        if await GoalRepository(self.db).active_outreach(dm_session) is None:
            return
        tools = {t.name: t.handler for t in
                 make_outreach_reply_tools(self.ctx, self.bridge, dm_session)}
        digits = dm_session.rsplit(":", 1)[-1]
        who = next((p.name for p in self.personas
                    if p.phone_digits == digits), "someone")
        await tools["finish_outreach"](result=f"{who} confirmed Thursday")

    # ------------------------------------------------------------------
    async def announce(self) -> None:
        from server.services.whatsapp_outreach_tools import (
            make_group_send_tools,
        )

        tools = {t.name: t.handler for t in make_group_send_tools(
            self.ctx, self.bridge, WORK_KEY)}
        out = json.loads(await tools["send_whatsapp_group_message"](
            group_id="doom",
            message="Team lunch is booked for Thursday 1pm at Bistro — "
                    "see you there!",
            goal_id=self.bb["root"]))
        assert out["ok"], out

    async def book(self) -> None:
        """Voice leg, scripted at the call-result layer: the real result →
        settle → roll-up → wake → event-memory chain runs end to end."""
        from server.repositories.phone_calls import PhoneCallRepository
        from server.services import (
            goal_service,
            phone_call_result_service as prs,
        )

        await self.db.execute(
            "INSERT INTO subagents (id, parent_session_key, session_key, task, "
            "status, agent_type, created_at, updated_at) VALUES "
            "('sub-book', ?, 'subagent:call:1', 'call the bistro', 'running', "
            "'openai_voice', datetime('now'), datetime('now'))",
            (WORK_KEY,))
        call_goal = await goal_service.create_goal(
            self.ctx, conversation_id="subagent:call:1",
            objective="Call the bistro and book Thursday 1pm for 8",
            origin_conversation_id=WORK_KEY, kind="call",
            external_ref="sub-book", parent_goal_id=self.bb["book"])
        self.bb["call_marker"] = f"callres-{call_goal['id'][:8]}"

        async def _fake_get(self, call_id):
            return {"subagent_id": "sub-book"}

        async def _summary(ctx, call_id, agenda, status):
            return "Bistro booked Thursday 1pm for 8, name Bob, no deposit."

        with self.monkeypatch.context() as mp:
            mp.setattr(PhoneCallRepository, "get", _fake_get)
            mp.setattr(prs, "generate_call_summary", _summary)
            mp.setattr(prs, "_append_call_event", _noop())
            await prs.dispatch_call_result(
                self.ctx, call_id="call-book", origin_session_key=WORK_KEY,
                agenda="book the bistro", status="completed")

    async def approve(self) -> None:
        """Mike's affirmative reply (respond_approval), then Bob's skill
        turn: the printful skill places the order against the stub and the
        merch child settles through the chokepoint. The skill itself lives
        in the workspace, so the harness simulates its HTTP behaviour —
        same key discipline, same external_id idempotency."""
        from server.repositories.approvals import ApprovalRepository
        from server.services.approval_tools import make_approval_tools

        row = (await ApprovalRepository(self.db).pending())[0]
        tools = {t.name: t.handler for t in
                 make_approval_tools(self.ctx, WORK_KEY)}
        out = json.loads(await tools["respond_approval"](
            approval_id=row["id"], decision="approve"))
        assert out["ok"], out

        from tests.rehearsal.pod_stub import stub_client
        cart = dict(self.bb["cart"])
        cart["external_id"] = f"bob-{row['id']}"
        async with stub_client(self.pod_stub) as client:
            r = await client.post(
                "http://stub/v2/orders", json=cart,
                headers={"Authorization": "Bearer rehearsal-key"})
            assert r.status_code < 400, r.text
            order_id = r.json()["result"]["id"]
        from server.services import goal_service
        await goal_service.settle_goal(
            self.ctx, self.bb["merch"], status="completed",
            result=f"POD order {order_id} placed via the printful skill")

    # ------------------------------------------------------------------
    async def _status(self, goal_id: str):
        from server.repositories.goals import GoalRepository
        row = await GoalRepository(self.db).get(goal_id)
        return row["status"] if row else None

    def _status_is(self, goal_id: str, *statuses: str):
        async def _cond() -> bool:
            return await self._status(goal_id) in statuses
        return _cond

    async def _pending_purchase(self) -> bool:
        from server.repositories.approvals import ApprovalRepository
        return any(r["approval_type"] == "purchase"
                   for r in await ApprovalRepository(self.db).pending())

    async def _headcount_updated(self) -> bool:
        from server.repositories.goals import GoalRepository
        row = await GoalRepository(self.db).get(self.bb["root"])
        state = json.loads((row or {}).get("strategy_json") or "{}")
        known = " ".join(state.get("known") or [])
        return "headcount updated" in known or bool(state.get("cancelled"))

    async def run(self) -> RehearsalScore:
        try:
            return await self._run_inner()
        finally:
            self._restore_executors()

    async def _run_inner(self) -> RehearsalScore:
        await self.setup()
        await self.kickoff()

        for tick in range(1, 6):
            await self.driver.deliver_due(tick, self.inject)
            await self.settle_ticks()
            if await self._status(self.bb["negotiate"]) != "active":
                break
        await _wait_for(self._status_is(self.bb["negotiate"],
                                        "completed", "failed", "cancelled"),
                        what="negotiate settled on quorum")

        if self.perturbation == "late_cancel":
            # Arm BEFORE the announcement — carol cancels in reply to it.
            self.driver.arm_cancellation()

        await self.announce()

        await self.book()
        await self.settle_ticks()
        await _wait_for(self._status_is(self.bb["book"], "completed"),
                        what="book child completed")
        await _wait_for(self._pending_purchase,
                        what="purchase approval requested")

        await self.approve()
        await self.settle_ticks()
        await _wait_for(self._status_is(self.bb["merch"], "completed"),
                        what="merch order placed and child settled")

        if self.perturbation == "late_cancel":
            await self.driver.deliver_due(100, self.inject)   # carol cancels
            await self.settle_ticks()
            await _wait_for(self._headcount_updated,
                            what="cancellation → headcount update")

        await asyncio.sleep(1.1)  # the compressed T-2h reminder becomes due
        from server.services.goal_service import pump_due_wakeups
        self.score.reminder_wakeups_fired = await pump_due_wakeups(self.ctx)
        await asyncio.sleep(0.1)  # drain detached dispatch tasks before close

        await self._score_facts()
        self.score.unnecessary_wakes = self.actors.unnecessary_wakes
        self.score.orders = len(self.pod_state["orders"])
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM effects WHERE kind = 'goal_revise_state'")
        self.score.reviser_runs = int(row["n"]) if row else 0
        return self.score

    async def _score_facts(self) -> None:
        """Formal information-loss metric (§4.3): every scripted ground-truth
        fact must be reflected in goal state (or event memory) and must have
        arrived via a real revision — not just at final state by accident."""
        from server.repositories.goals import GoalRepository

        repo = GoalRepository(self.db)
        states = []
        for gid in (self.bb["negotiate"], self.bb["root"]):
            row = await repo.get(gid)
            states.append((row or {}).get("strategy_json") or "")
            if row and row.get("result"):
                states.append(row["result"])
        # Dietary facts may legitimately land in any goal the router deemed
        # relevant (ordering between negotiate settling and the batch's
        # arrival is free); what must never happen is the fact reaching NO
        # goal state at all.
        for row in await self.db.fetch_all(
                "SELECT strategy_json FROM goals "
                "WHERE strategy_json LIKE '%vegetarian%'"):
            states.append(row["strategy_json"] or "")
        combined = "\n".join(states)

        cancelled_persona = "carol" if self.perturbation == "late_cancel" else None
        for p in self.personas:
            if p.name == cancelled_persona:
                if f"person-{p.name}" not in combined:
                    self.score.incidents.append(
                        f"{p.name}'s cancellation never reached goal state")
                continue
            if f"person-{p.name}" not in combined:
                self.score.incidents.append(
                    f"{p.name}'s attendance never reached goal state")
        if "vegetarian" not in combined:
            self.score.incidents.append("eve's dietary fact (DM) was lost")

        ev = await self.db.fetch_one(
            "SELECT strategy_json FROM goals WHERE kind = 'event_plan'")
        if ev and "Thursday" not in (ev["strategy_json"] or "") + combined:
            # the decided slot is in the template plan text; only flag when
            # nothing anywhere mentions it
            if "Thursday" not in combined:
                self.score.incidents.append("decided slot lost")

        if not await self.db.fetch_one(
                "SELECT 1 FROM effects WHERE kind = 'goal_revise_state'"):
            self.score.incidents.append("no reviser runs recorded")

    def _restore_executors(self) -> None:
        from server.services import effects as effects_svc
        if getattr(self, "_saved_send", None) is not None:
            effects_svc._EXECUTORS["whatsapp_send"] = self._saved_send
        # Cross-test hygiene: nothing module-level may outlive the scenario,
        # or later tests in the same process inherit stale loop-bound state.
        from server.services import wake_service as ws
        ws._pending_dispatches.clear()
        from server.services import goal_state_service as gss
        gss._GOAL_LOCKS.clear()
        gss._REVISER_SEMAPHORES.clear()
