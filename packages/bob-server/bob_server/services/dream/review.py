"""Retrospective pass: one session window → validated candidates."""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any

from bob_server.context import AppContext
from bob_server.services.base import BaseService
from bob_server.services.dream.models import Evidence, PlanCandidate, ResolutionCandidate

logger = logging.getLogger(__name__)

_LINE_TRUNCATE = 400


def _slug(name: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", name.strip().lower())).strip("-")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _excerpt_matches(excerpt: str, line_content: str) -> bool:
    """Fuzzy containment: the cited excerpt must come from the cited line."""
    a, b = _normalise(excerpt), _normalise(line_content)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.7


class ReviewService(BaseService):
    """Builds transcripts, calls the review LLM, validates candidates."""

    async def build_roster(self, session_key: str) -> tuple[str, dict[str, str]]:
        """Roster header + sender_id→name map for the session's contacts."""
        from bob_server.repositories.contacts import ContactRepository
        rows = await ContactRepository(self.db).list_active()
        names: dict[str, str] = {}
        lines = []
        for r in rows or []:
            name = (r["name"] or "").strip()
            names[r["id"]] = name
            display = name or r["id"]
            lines.append(f"{r['id']}|{display}|person-{_slug(display)}")
        roster = "\n".join(lines)
        header = "Roster (sender_id|name|entity_id):\n" + roster if roster else "Roster: (no contacts)"
        return header, names

    def build_transcript(self, messages: list[dict], sender_names: dict[str, str] | None = None) -> list[str]:
        """Numbered transcript lines. Assistant messages are tagged [BOB]; user
        messages with the speaker's roster name when the sender is known."""
        names = sender_names or {}
        lines = []
        for i, m in enumerate(messages, 1):
            role = m.get("role", "user")
            content = (m.get("content") or "").replace("\n", " ")[:_LINE_TRUNCATE]
            if role == "assistant":
                lines.append(f"[{i}] [BOB] {content}")
            else:
                name = names.get(str(m.get("sender_id") or ""))
                tag = f"[{name}]" if name else "[user]"
                lines.append(f"[{i}] {tag} {content}")
        return lines

    async def review_session(
        self,
        *,
        session_key: str,
        messages: list[dict],
        group_hint: str = "",
    ) -> dict[str, Any]:
        """One LLM review call → validated candidates + stats."""
        from bob_server.services.dream.prompts import REVIEW_SYSTEM
        from bob_server.services.llm_dispatch import LLMDispatchService

        stats: dict[str, Any] = {
            "session_key": session_key,
            "messages": len(messages),
            "resolutions_proposed": 0,
            "plans_proposed": 0,
            "rejected_bad_json": 0,
            "rejected_invalid": 0,
        }
        if len(messages) < 2:
            return {"resolutions": [], "plans": [], "stats": stats}

        roster_header, names = await self.build_roster(session_key)
        transcript = self.build_transcript(messages, names)
        user_prompt = (
            f"Session: {session_key}\n{group_hint}\n{roster_header}\n\n"
            f"Transcript ({len(transcript)} lines):\n" + "\n".join(transcript)
        )

        llm = LLMDispatchService(self.ctx)
        model = self.ctx.settings.openai.get_memory_model()
        raw = await self._chat_json(
            llm,
            system=REVIEW_SYSTEM,
            user=user_prompt,
            call_category="dream_review",
            session_key=session_key,
            model=model,
        )
        if raw is None:
            stats["rejected_bad_json"] = 1
            return {"resolutions": [], "plans": [], "stats": stats}

        by_index = {i: m for i, m in enumerate(messages, 1)}
        resolutions: list[ResolutionCandidate] = []
        plans: list[PlanCandidate] = []

        for item in raw.get("resolutions", []) or []:
            cand, err = self._validate_resolution(item, by_index, session_key)
            if cand:
                resolutions.append(cand)
            elif err:
                stats["rejected_invalid"] += 1
                logger.debug("dream review rejected resolution: %s", err)

        for item in raw.get("plans", []) or []:
            cand, err = self._validate_plan(item, by_index, session_key)
            if cand:
                plans.append(cand)
            elif err:
                stats["rejected_invalid"] += 1
                logger.debug("dream review rejected plan: %s", err)

        stats["resolutions_proposed"] = len(resolutions)
        stats["plans_proposed"] = len(plans)
        return {"resolutions": resolutions, "plans": plans, "stats": stats}

    # ----------------------------------------------------------- validation

    def _validate_evidence(
        self, raw_evidence: Any, by_index: dict[int, dict], session_key: str, run_id: str = ""
    ) -> list[Evidence]:
        out: list[Evidence] = []
        if not isinstance(raw_evidence, list):
            return out
        for ev in raw_evidence[:5]:
            if not isinstance(ev, dict):
                continue
            line = ev.get("line")
            excerpt = str(ev.get("excerpt", ""))[:300]
            if not isinstance(line, int) or line not in by_index:
                continue
            msg = by_index[line]
            if not _excerpt_matches(excerpt, msg.get("content") or ""):
                continue
            out.append(
                Evidence(
                    kind="observed",
                    session_key=session_key,
                    line=line,
                    excerpt=excerpt,
                    at=str(msg.get("created_at", "")),
                    by=str(msg.get("sender_id", "") or ""),
                    run_id=run_id,
                )
            )
        return out

    def _validate_resolution(
        self, item: Any, by_index: dict[int, dict], session_key: str
    ) -> tuple[ResolutionCandidate | None, str | None]:
        if not isinstance(item, dict):
            return None, "not an object"
        fields = [str(item.get(k, "") or "").strip() for k in ("title", "behaviour", "trigger_condition", "success_signal")]
        if not all(fields):
            return None, "missing required field"
        title, behaviour, trigger, signal = fields
        if len(signal) < 15:
            return None, "vacuous success signal"
        evidence = self._validate_evidence(item.get("evidence"), by_index, session_key)
        if not evidence:
            return None, "no valid evidence citation"
        return (
            ResolutionCandidate(
                title=title[:200], behaviour=behaviour[:1000],
                trigger_condition=trigger[:500], success_signal=signal[:1000],
                evidence=evidence,
            ),
            None,
        )

    def _validate_plan(
        self, item: Any, by_index: dict[int, dict], session_key: str
    ) -> tuple[PlanCandidate | None, str | None]:
        if not isinstance(item, dict):
            return None, "not an object"
        fields = [str(item.get(k, "") or "").strip() for k in ("title", "what_was_discussed", "proposed_action", "assistance_method")]
        if not all(fields):
            return None, "missing required field"
        title, discussed, action, assist = fields
        evidence = self._validate_evidence(item.get("evidence"), by_index, session_key)
        if not evidence:
            return None, "no valid evidence citation"
        related = item.get("related_entities") or []
        related_entities = [str(e) for e in related if isinstance(e, str) and e.startswith(("person-", "group-"))][:8]
        tier = item.get("autonomy_tier", 1)
        return (
            PlanCandidate(
                title=title[:200], what_was_discussed=discussed[:1000],
                proposed_action=action[:1000], assistance_method=assist[:1000],
                autonomy_tier=2 if tier == 2 else 1,
                due_hint=str(item.get("due_hint", "") or "")[:200],
                evidence=evidence, related_entities=related_entities,
            ),
            None,
        )

    # ------------------------------------------------------------ llm helper

    async def _chat_json(
        self, llm: Any, *, system: str, user: str, call_category: str,
        session_key: str, model: str,
    ) -> dict | None:
        """Chat call expecting strict JSON; one retry on parse failure."""
        for attempt in range(2):
            extra = "\n\nYour previous reply was not valid JSON. Reply with ONLY the JSON object." if attempt else ""
            response = await llm.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user + extra},
                ],
                call_category=call_category,
                session_key=session_key,
                model=model,
                temperature=0.3,
            )
            parsed = self._parse_json(response)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _parse_json(text: str | None) -> dict | None:
        if not text:
            return None
        candidate = text.strip()
        fence = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL)
        if fence:
            candidate = fence.group(1).strip()
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(candidate[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None
