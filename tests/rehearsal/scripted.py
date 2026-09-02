"""Scripted actors — deterministic LLM stand-ins driving the REAL pipeline.

Interception happens at ``LLMDispatchService.chat`` / ``.chat_with_tools``
so everything underneath is real: the tool loop's handlers, effects,
extraction tools, the router, reviser CAS writes, and wake dispatches. The
stand-ins may cheat on READING (they consult the DB directly for history)
but every WRITE goes through the production tools/services.

- ``memory_silent_turn``  → the extractor: turns persona messages into
  add_claim calls, REUSING the candidate entity from the §2.0 prompt block
  (or minting a wrong slug when ``wrong_slug`` is set — the perturbation).
- ``goal_revise``         → the reviser: folds routed claims / child results
  into state, evaluates the quorum decision rule, wakes on change only.
- generic dispatch        → the main model: reacts to wake content by
  calling the real goal/approval tools.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


def _last_user(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content") or ""
    return ""


class ScriptedActors:
    def __init__(self, db: Any, roster: list[str], quorum: float = 0.75, *,
                 wrong_slug: bool = False, blackboard: dict | None = None):
        self.db = db
        self.roster = roster                # person slugs, e.g. "person-alice"
        self.quorum = quorum
        self.wrong_slug = wrong_slug
        # NB: identity check, not `or {}` — the caller's dict starts empty and
        # is populated later; a falsy-default would silently fork it.
        self.blackboard = blackboard if blackboard is not None else {}
        self._slug_cache: dict[str, str] = {}
        self.main_calls: list[str] = []
        self.unnecessary_wakes = 0

    def install(self, monkeypatch) -> None:
        from server.services.llm_dispatch import LLMDispatchService
        monkeypatch.setattr(LLMDispatchService, "chat", self._chat)
        monkeypatch.setattr(
            LLMDispatchService, "chat_with_tools", self._chat_with_tools)

    # ------------------------------------------------------------------
    async def _chat(self, messages, **kw) -> str:
        category = kw.get("call_category", "")
        if category == "goal_revise":
            return await self._reviser(messages)
        if category == "attention_probe":
            return json.dumps({"decision": "ACT", "reason": "scripted"})
        if category == "claim_router_probe":
            return json.dumps({"verdict": "RELEVANT"})
        if category == "call_summary":
            return "Restaurant booked Thursday 1pm for 8, name Bob, no deposit."
        return "{}"

    async def _chat_with_tools(self, messages, tools, **kw) -> str:
        handlers = {t.name: t.handler for t in tools}
        category = kw.get("call_category", "")
        if category == "memory_silent_turn":
            return await self._extractor(messages, handlers, kw)
        return await self._main_model(messages, handlers, kw)

    # ------------------------------------------------------------------
    async def _slug_for(self, contact_id: str) -> str:
        if contact_id in self._slug_cache:
            return self._slug_cache[contact_id]
        row = await self.db.fetch_one(
            "SELECT name FROM contacts WHERE id = ?", (contact_id,))
        slug = re.sub(r"[^a-z0-9]+", "-",
                      (row["name"] if row else contact_id).lower()).strip("-")
        self._slug_cache[contact_id] = slug
        return slug

    async def _extractor(self, messages, handlers, kw) -> str:
        session_key = kw.get("session_key") or ""
        # The §2.0 candidate block rides the instruction (user turn); the
        # claim-type rules ride the system prompt — scan both, like a real
        # model reading the whole prompt would.
        prompt_text = "\n".join(m.get("content") or "" for m in messages
                                if m.get("role") in ("system", "user"))
        candidates = re.findall(r"`(event-[a-z0-9-]+)`", prompt_text)
        subject = ("event-lunch" if self.wrong_slug
                   else (candidates[0] if candidates else "event-lunch"))

        from server.repositories.history import HistoryRepository
        rows = await HistoryRepository(self.db).recent_dialogue(
            session_key, limit=40, dispatched_only=False)

        ops: list[tuple[str, dict[str, Any]]] = []
        needed = {subject}
        for row in rows or []:
            if row["role"] != "user":
                continue
            text = (row["content"] or "").lower()
            slug = await self._slug_for(row["sender_id"] or "")
            if not slug:
                continue
            person = f"person-{slug}"
            if any(p in text for p in ("i'm in", "im in", "count me in")):
                ops.append(("add_claim", {
                    "subject_id": subject, "claim_type_key": "attendee",
                    "object_id": person}))
                needed.add(person)
            if any(p in text for p in ("can't make it", "cant make it", "drop out")):
                ops.append(("add_claim", {
                    "subject_id": person, "claim_type_key": "truth",
                    "value": f"not attending {subject}"}))
                needed.add(person)
            if "vegetarian" in text:
                ops.append(("add_claim", {
                    "subject_id": person, "claim_type_key": "food_preference",
                    "value": "vegetarian"}))
                needed.add(person)

        for eid in sorted(needed):
            if "create_entity" not in handlers:
                break
            exists = await self.db.fetch_one(
                "SELECT 1 FROM memory_entities WHERE entity_id = ?", (eid,))
            if not exists:
                await handlers["create_entity"](
                    entity_id=eid,
                    entity_type="person" if eid.startswith("person-") else "event")
        written = 0
        for name, args in ops:
            if name in handlers:
                await handlers[name](**args)
                written += 1
        return f"[scripted extraction: {written} claim(s) on {subject}]"

    # ------------------------------------------------------------------
    def _parse_state(self, prompt: str) -> dict[str, Any]:
        # Everything between the two markers is one complete JSON object; a
        # non-greedy brace match would stop at the first nested '}' and fall
        # back to an empty state — silently destroying refs on every write.
        m = re.search(r"# Current state\s*(.*?)\s*# Stimulus", prompt, re.S)
        if not m:
            return {"v": 2}
        try:
            state = json.loads(m.group(1))
            return state if isinstance(state, dict) else {"v": 2}
        except ValueError:
            return {"v": 2}

    async def _reviser(self, messages) -> str:
        prompt = _last_user(messages)
        state = self._parse_state(prompt)
        stimulus = prompt.split("# Stimulus", 1)[-1]
        pre_state = json.dumps(state, sort_keys=True, default=str)

        # Attendance/quorum semantics apply to the plan goals; an outreach
        # goal receiving the same routed batch must not start counting
        # quorum (it would wake the target DM for no reason).
        m = re.match(r"# Goal \((\w+)\)", prompt)
        goal_kind = m.group(1) if m else ""
        tracks_attendance = goal_kind in ("negotiate", "event_plan")

        attendees = set(state.get("attendees") or []) if tracks_attendance \
            else set()
        prev_cancelled = set(state.get("cancelled") or []) if tracks_attendance \
            else set()
        cancelled = set(prev_cancelled)
        roster = set(self.roster) if tracks_attendance else set()

        # Router stimulus lines: "- event-x (...): attendee → person-alice"
        for slug in re.findall(r"attendee → (person-[a-z0-9-]+)", stimulus):
            attendees.add(slug)
        # Child-settle results: "Result: alice confirmed Thursday"
        for name in re.findall(r"Result: ([a-z0-9]+) confirmed", stimulus):
            attendees.add(f"person-{name}")
        # Cancellations (truth claims routed from any channel)
        for slug in re.findall(r"(person-[a-z0-9-]+): truth = not attending",
                               stimulus):
            attendees.discard(slug)
            cancelled.add(slug)

        count = len(attendees & roster)
        needed = self.quorum * len(roster)
        quorum_reached = count >= needed
        wake, summary = False, ""

        known = [k for k in (state.get("known") or [])
                 if not k.startswith(("vegetarian:", "decided slot"))
                 and not re.match(r"^\d+/\d+ invitees confirmed", k)]
        for slug, pref in re.findall(
                r"(person-[a-z0-9-]+)[^\n]*food_preference = (\w+)", stimulus):
            known.append(f"vegetarian: {slug.replace('person-', '')} "
                         f"({pref})" if pref == "vegetarian" else
                         f"{pref}: {slug.replace('person-', '')}")
        if "confirmed thursday" in stimulus.lower() and not any(
                "decided slot" in k for k in known):
            known.append("decided slot: Thursday (from confirmations)")
        next_actions: list[dict[str, Any]] = list(state.get("next_actions") or [])

        if quorum_reached and int(state.get("attendee_count") or 0) < needed:
            wake, summary = True, f"quorum reached ({count}/{len(roster)})"
            next_actions = [{"action":
                             f"settle the negotiate goal: quorum reached "
                             f"({count}/{len(roster)}); announce the slot",
                             "due": ""}]
        elif cancelled != prev_cancelled:
            wake, summary = True, "headcount changed: a confirmed attendee dropped"
            next_actions = [{"action":
                             "call the restaurant to update the headcount",
                             "due": ""}]
        elif "booked" in stimulus.lower() and not any(
                "booking" in (na.get("action") or "")
                for na in next_actions):
            # A booking call result rolled up: the working conversation must
            # write the event and schedule reminders. Skip when the action is
            # already queued (duplicate roll-ups must not re-wake).
            wake, summary = True, "venue booked — process the booking result"
            next_actions = list(next_actions) + [
                {"action": "process the booking result", "due": ""}]
        elif quorum_reached:
            next_actions = [{"action": "settle the negotiate goal: quorum reached",
                             "due": ""}]
        if wake:
            # The metric: a wake is unnecessary when the reviser changed
            # NOTHING in the state (empty diff) yet still asked for a turn.
            post_state = json.dumps(state, sort_keys=True, default=str)
            if post_state == pre_state:
                self.unnecessary_wakes += 1
                import os as _os
                if _os.getenv('REH_DEBUG'):
                    print(f'UNNECESSARY kind={goal_kind} '
                          f'stimulus={stimulus[:120]!r}', flush=True)

        import os as _os
        if _os.getenv('REH_DEBUG'):
            print(f'REVISER kind={goal_kind} wake={wake} '
                  f'summary={summary!r} stim={stimulus[:80]!r}', flush=True)
        state.update({
            "attendees": sorted(attendees & (roster | attendees)),
            "attendee_count": count,
            "cancelled": sorted(cancelled),
            "next_actions": next_actions,
            "known": ([f"{count}/{len(roster)} invitees confirmed"] + known
                      if attendees else known),
        })
        return json.dumps({"state": state, "wake_needed": wake,
                           "wake_summary": summary})

    # ------------------------------------------------------------------
    async def _main_model(self, messages, handlers, kw) -> str:
        """The main model reacts to its CURRENT GOAL STATE (via list_goals —
        exactly the goals_block a real dispatch sees), not to wake prose —
        deterministic regardless of which wake won the race."""
        self.main_calls.append((kw.get("session_key") or "")[:40])
        bb = self.blackboard

        async def call(name: str, **args):
            if name not in handlers:
                raise RuntimeError(f"scripted main model: tool {name!r} missing")
            return await handlers[name](**args)

        if "list_goals" not in handlers:
            return "Noted."
        listed = json.loads(await call("list_goals"))
        goals = {g["goal_id"]: g for g in listed["goals"]}
        import os as _os2
        if _os2.getenv('REH_DEBUG'):
            print('MAIN goals=' + str([
                (g['kind'], g['status'],
                 [na.get('action', '')[:30] for na in g.get('next_actions') or []])
                for g in listed['goals']])[:600], flush=True)

        def _actions(gid):
            g = goals.get(gid) or {}
            return " ".join(na.get("action", "") for na in g.get("next_actions") or [])

        def _known(gid):
            g = goals.get(gid) or {}
            return " ".join(g.get("known") or [])

        # 1) Booking result pending on the book child → full booking routine.
        if not bb.get("booking_done") and "process the booking" in _actions(bb.get("book", "")):
            bb["booking_done"] = True
            await call("complete_goal", goal_id=bb["book"],
                       result="Table booked Thursday 1pm for 8.")
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            await call("schedule_goal_wakeup", goal_id=bb["remind"],
                       not_before=now.isoformat(), note="T-24h reminder")
            await call("schedule_goal_wakeup", goal_id=bb["remind"],
                       not_before=(now + timedelta(seconds=1)).isoformat(),
                       note="T-2h reminder")
            await self._write_event_memory(bb)
            await call("request_approval",
                       title="Team lunch merch order",
                       description="8 × official team tee",
                       approval_json=json.dumps({
                           "approval_type": "purchase",
                           "entity_id": bb["merch"],
                           "proposal": {"goal_id": bb["merch"],
                                        "summary": "8 × team tee",
                                        "order": bb["cart"]}}))
            return ("Booked; reminders scheduled; event written to memory; "
                    "approval requested.")

        # 2) Quorum reached on the negotiate child → settle it.
        if "settle the negotiate" in _actions(bb.get("negotiate", "")) \
                and goals.get(bb.get("negotiate", ""), {}).get("status") != "completed":
            await call("complete_goal", goal_id=bb["negotiate"],
                       result="Quorum reached; Thursday 1pm chosen.")
            return "Settled the negotiation."

        # 3) Cancellation rolled in → update the plan's headcount record.
        if "headcount" in _actions(bb.get("root", "")) and \
                "headcount updated" not in _known(bb.get("root", "")):
            from server.repositories.goals import GoalRepository
            row = await GoalRepository(self.db).get(bb["root"])
            if row:
                state = json.loads(row["strategy_json"] or "{}")
                state.setdefault("known", [])
                state["known"] = list(state["known"]) + [
                    "headcount updated with the restaurant after a cancellation"]
                state["next_actions"] = []
                await call("update_goal_state", goal_id=bb["root"],
                           expected_version=row["version"],
                           state=json.dumps(state))
            return "Headcount updated."

        # 4) Reminder wake → record progress on the remind child.
        text = _last_user(messages)
        if "T-24h reminder" in text or "T-2h reminder" in text:
            g = goals.get(bb.get("remind", "")) or {}
            if g:
                await call("update_goal", goal_id=bb["remind"],
                           expected_version=g.get("version") or 1,
                           progress=text[:40])
            return "Reminder processed."

        return "Noted."

    async def _write_event_memory(self, bb: dict[str, Any]) -> None:
        """The booking wake's model turn writes the event entity + claims —
        making the booking routable memory (§3.2)."""
        from datetime import datetime as _dt
        from server.services.memory.claim_service import write_claim
        from server.services.memory.models import Claim

        await self.db.execute(
            "INSERT OR IGNORE INTO memory_entities "
            "(entity_id, entity_type, display_name, status, created_at) "
            "VALUES ('event-team-lunch', 'event', 'Team Lunch', 'active', "
            "datetime('now'))")
        await self.db.execute(
            "INSERT OR IGNORE INTO memory_entities "
            "(entity_id, entity_type, display_name, status, created_at) "
            "VALUES ('location-bistro', 'location', 'Bistro', 'active', "
            "datetime('now'))")
        for ck, val, obj in (("name", "Team Lunch", None),
                             ("start_time", "2026-08-27T13:00", None),
                             ("location", None, "location-bistro")):
            await write_claim(self.db, Claim(
                id=f"claim-ev-{ck}", claim_type_key=ck,
                subject_id="event-team-lunch", value=val, object_id=obj,
                status="active",
                source_messages=[bb.get("call_marker", "x")],
                created_at=_dt.now()))

