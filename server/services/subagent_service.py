"""Subagent service — manages async subagent lifecycle with Claude Code CLI backend."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from server.services.base import BaseService, utcnow

logger = logging.getLogger(__name__)

# Module-level tracking for running async tasks and per-subagent locks
_running_tasks: dict[str, asyncio.Task[None]] = {}
_locks: dict[str, asyncio.Lock] = {}

# Aliases the parent LLM tends to invent for the openai_voice agent_type. We
# normalise to "openai_voice" rather than rejecting — historical data shows the
# LLM producing values like "phone-call", "voice_chat", "voice" unpredictably.
# Modality aliases live in voice_dispatch_service (shared with the outreach
# tools) so there is exactly one vocabulary table.
_VOICE_AGENT_TYPE_ALIASES = {
    "openai_voice", "voice_call", "voice_chat", "voice", "phone_call",
    "phone-call", "phone", "voice_link", "voip", "realtime",
}


def _normalise_voice_agent_type(value: str) -> str:
    v = (value or "").strip().lower()
    return "openai_voice" if v in _VOICE_AGENT_TYPE_ALIASES else value

SUBAGENT_SYSTEM_PROMPT = """\
You are a subagent of Bob, an AI assistant. You have been given a task to complete.
Use your available tools (Read, Write, Edit, Glob, Grep, Bash) to accomplish the task.
Your working directory is the workspace — write files here by default (no absolute paths needed).
Provide clear, concise output describing what you did and what the result is.
"""

LOCAL_SUBAGENT_SYSTEM_PROMPT = """\
You are a subagent of Bob. You have been assigned a task.
Use your available tools to accomplish it.
Your working directory is the workspace root.
Provide clear, concise output describing what you did and what the result is.
When done, output your final answer as plain text.
"""


def _get_lock(subagent_id: str) -> asyncio.Lock:
    if subagent_id not in _locks:
        _locks[subagent_id] = asyncio.Lock()
    return _locks[subagent_id]


class SubagentService(BaseService):
    """Manages async subagent lifecycle — create, run, message, check, list, kill."""

    def _repo(self):
        from server.repositories.subagents import SubagentRepository
        return SubagentRepository(self.db)

    async def create_subagent(
        self,
        task: str,
        parent_session_key: str,
        *,
        agent_type: str = "claude",
        persona: bool = False,
        model: str = "",
        contact_id: str | None = None,
        modality: str = "phone",
        goal_parent_id: str | None = None,
    ) -> dict[str, Any]:
        # Normalise voice-agent aliases the parent LLM invents. Anything in the
        # voice vocabulary routes to the openai_voice path; the stored values
        # are the canonical ones so dashboards/logs are consistent. As a stronger
        # signal: if contact_id is present we treat the subagent as a voice call
        # regardless of agent_type — the LLM keeps inventing new type names
        # ("phone-call", "voice_chat", "general-purpose", …) and contact_id is
        # the unambiguous "this is a call to a person" marker.
        from server.services.voice_dispatch_service import normalise_voice_modality

        requested_modality = modality
        normalised_type = _normalise_voice_agent_type(agent_type)
        if normalised_type == "openai_voice" or (contact_id and agent_type not in ("claude", "local", "script")):
            agent_type = "openai_voice"
            # Unknown modality vocabulary defaults to phone — never guess toward
            # a modality the caller didn't clearly pick... except that bare
            # "voice" et al. are voice_link (see the shared alias table).
            # Unknown modality vocabulary defaults to phone — see the shared
            # alias table in voice_dispatch_service for the known vocabulary.
            modality = normalise_voice_modality(modality) or "phone"

        subagent_id = str(uuid4())
        short_id = subagent_id[:8]
        session_key = f"subagent:{parent_session_key}:{short_id}"
        now = utcnow().isoformat()

        await self._repo().insert(
            subagent_id=subagent_id, parent_session_key=parent_session_key,
            session_key=session_key, task=task, agent_type=agent_type,
            persona=int(persona), model=model, contact_id=contact_id,
            modality=modality, now_iso=now)

        # Bob3 Phase V: a subagent is a goal held on behalf of the parent
        # conversation. Completion settles the goal and wakes the parent.
        try:
            from server.services.goal_service import create_goal
            await create_goal(
                self.ctx,
                conversation_id=session_key,
                objective=task[:2000],
                origin_conversation_id=parent_session_key,
                kind="call" if agent_type == "openai_voice" else "subagent",
                external_ref=subagent_id,
                parent_goal_id=goal_parent_id,
            )
        except Exception:
            logger.warning("failed to create goal for subagent %s", short_id, exc_info=True)

        # openai_voice dispatches synchronously so we can return voice_url / call_sid
        # to the LLM in the tool result. No background task — the row stays in
        # 'running' until VoiceSessionService.complete or _run_realtime_call marks
        # it completed when the call ends.
        if agent_type == "openai_voice":
            try:
                dispatch = await self._dispatch_openai_voice(
                    subagent_id, task, contact_id, modality, parent_session_key,
                )
            except Exception as e:
                logger.warning("openai_voice subagent %s dispatch failed: %s", short_id, e)
                from server.services import occupancy
                occupancy.mark_idle_by_ref(subagent_id)
                await self._update_status(subagent_id, "failed", error=str(e))
                return {"ok": False, "error": str(e), "subagent_id": subagent_id, "session_key": session_key}

            logger.info("openai_voice subagent %s dispatched: %s", short_id, dispatch)
            result = {
                "ok": True,
                "subagent_id": subagent_id,
                "session_key": session_key,
                "status": "running",
                "modality": modality,
                **dispatch,
            }
            # Surface alias coercions so the LLM can notice and retry rather
            # than conclude the tool is broken (observed 2026-08-14: it asked
            # for a phone call via modality="voice", got a link, and told the
            # user the dialler didn't work).
            if requested_modality and requested_modality.strip().lower() != modality:
                result["requested_modality"] = requested_modality
                result["note"] = (
                    f"you asked for modality '{requested_modality}' which was "
                    f"interpreted as '{modality}' — pass modality='phone' or "
                    f"'voice_link' explicitly to be sure"
                )
            return result

        # Bob3 Phase VI item 5: the spawn is a durable effect — claude/local
        # are executor kinds on the SpawnSubagent record. The executor starts
        # the background run; the pump can re-deliver after a crash (the
        # executor guards against re-spawning finished or running subagents).
        from server.services import effects as effects_svc
        spawn = await effects_svc.emit_and_deliver(
            self.ctx,
            kind="subagent_spawn",
            idempotency_key=f"subagent_spawn:{subagent_id}",
            payload={"subagent_id": subagent_id, "task": task,
                     "executor": agent_type,
                     "parent_session_key": parent_session_key},
        )
        if not spawn.get("ok"):
            await self._update_status(subagent_id, "failed",
                                      error=str(spawn.get("error")))
            return {"ok": False, "error": str(spawn.get("error")),
                    "subagent_id": subagent_id, "session_key": session_key}

        logger.info("Subagent created: id=%s session=%s", short_id, session_key)
        return {
            "ok": True,
            "subagent_id": subagent_id,
            "session_key": session_key,
            "status": "created",
        }

    async def _dispatch_openai_voice(
        self,
        subagent_id: str,
        task: str,
        contact_id: str | None,
        modality: str,
        parent_session_key: str,
    ) -> dict[str, Any]:
        """Dispatch a voice_link or phone call for this subagent.

        Thin delegation to VoiceDispatchService, which owns contact resolution,
        instruction building, and Twilio placement.
        """
        from server.services.voice_dispatch_service import VoiceDispatchService

        return await VoiceDispatchService(self.ctx).dispatch_contact_call(
            subagent_id, task, contact_id, modality, parent_session_key,
        )

    async def _run_subagent(self, subagent_id: str, task: str) -> None:
        short_id = subagent_id[:8]
        await self._update_status(subagent_id, "running")

        row = await self._repo().get(subagent_id)
        agent_type = row["agent_type"] if row else "claude"
        session_key = row["session_key"] if row else ""
        persona = bool(row["persona"]) if row else False
        model = row["model"] if row else ""

        settings = self._get_settings()

        try:
            if agent_type == "local":
                # Store user message in session before execution
                from server.services.session_service import SessionService
                await SessionService(self.ctx).add_message(
                    session_key, "user", task, channel="subagent",
                )
                result = await self._run_local(
                    session_key=session_key,
                    persona=persona,
                    model=model,
                )
            elif agent_type == "script":
                from server.services.session_service import SessionService
                await SessionService(self.ctx).add_message(
                    session_key, "user", task, channel="subagent",
                )
                result = await self._run_script(task)
            else:
                workspace_dir = settings.harness.workspace_dir.expanduser().resolve()
                result = await self._run_claude(
                    prompt=task,
                    cwd=workspace_dir,
                    max_budget=settings.harness.skill_dev_max_budget_usd,
                )
        except Exception as e:
            logger.error("Subagent %s failed: %s", short_id, e)
            await self._update_status(subagent_id, "failed", error=str(e))
            await self._notify_parent(subagent_id, f"ERROR: {e}", failed=True)
            _running_tasks.pop(subagent_id, None)
            return

        claude_session_id = result.get("session_id", "")
        result_text = result.get("result", "")
        cost = result.get("cost_usd", 0)

        now = utcnow().isoformat()
        await self._repo().store_result(
            subagent_id, result=result_text, claude_session_id=claude_session_id,
            cost_usd=cost, now_iso=now)

        # Store assistant message in subagent session
        # (user message already stored before execution for local, or stored here for claude)
        from server.services.session_service import SessionService
        session_svc = SessionService(self.ctx)
        if agent_type not in ("local", "script"):
            await session_svc.add_message(session_key, "user", task, channel="subagent")
        await session_svc.add_message(session_key, "assistant", result_text, channel="subagent")

        await self._publish_event(subagent_id, "result_ready")
        await self._notify_parent(subagent_id, result_text)

        logger.info(
            "Subagent %s waiting for parent: cost=%.4f chars=%d",
            short_id, cost, len(result_text),
        )
        _running_tasks.pop(subagent_id, None)

    async def message_subagent(self, subagent_id: str, message: str, *,
                               parent_session_key: str = "") -> dict[str, Any]:
        row = await self._repo().get(subagent_id)
        if row is None:
            return {"ok": False, "error": "Subagent not found"}
        if parent_session_key and row["parent_session_key"] != parent_session_key:
            # Don't confirm the existence of another session's subagent.
            return {"ok": False, "error": "Subagent not found"}
        if row["agent_type"] == "openai_voice":
            return {"ok": False, "error": "Voice subagent in progress; cannot message. Use kill_subagent to cancel."}
        if row["status"] not in ("waiting_for_parent", "running"):
            return {"ok": False, "error": f"Subagent is in status '{row['status']}', cannot receive messages"}

        agent_type = row["agent_type"]
        persona = bool(row["persona"])
        session_key = row["session_key"]
        model = row["model"]

        async with _get_lock(subagent_id):
            await self._update_status(subagent_id, "running")

            settings = self._get_settings()

            # Store user message before execution (local needs it in session history)
            from server.services.session_service import SessionService
            session_svc = SessionService(self.ctx)
            if agent_type == "local":
                await session_svc.add_message(session_key, "user", message, channel="subagent")

            try:
                if agent_type == "local":
                    result = await self._run_local(
                        session_key=session_key,
                        persona=persona,
                        model=model,
                    )
                else:
                    workspace_dir = settings.harness.workspace_dir.expanduser().resolve()
                    result = await self._run_claude(
                        prompt=message,
                        cwd=workspace_dir,
                        session_id=row["claude_session_id"],
                        max_budget=settings.harness.skill_dev_max_budget_usd,
                    )
            except Exception as e:
                await self._update_status(subagent_id, "failed", error=str(e))
                return {"ok": False, "error": str(e), "subagent_id": subagent_id}

            result_text = result.get("result", "")
            claude_session_id = result.get("session_id", row["claude_session_id"])
            cost = result.get("cost_usd", 0)
            total_cost = (row["cost_usd"] or 0) + cost

            now = utcnow().isoformat()
            await self._repo().store_result(
                subagent_id, result=result_text, claude_session_id=claude_session_id,
                cost_usd=total_cost, now_iso=now)

            # Store messages in subagent session
            if agent_type != "local":
                await session_svc.add_message(session_key, "user", message, channel="subagent")
            await session_svc.add_message(session_key, "assistant", result_text, channel="subagent")

            await self._publish_event(subagent_id, "result_ready")
            await self._notify_parent(subagent_id, result_text)

            logger.info("Subagent %s messaged: cost=%.4f", subagent_id[:8], total_cost)
            return {"ok": True, "result": result_text, "subagent_id": subagent_id}

    async def check_subagent(self, subagent_id: str, *,
                             parent_session_key: str = "") -> dict[str, Any]:
        row = await self._repo().get(subagent_id)
        if row is None:
            return {"ok": False, "error": "Subagent not found"}
        if parent_session_key and row["parent_session_key"] != parent_session_key:
            # Don't confirm the existence of another session's subagent.
            return {"ok": False, "error": "Subagent not found"}
        return {
            "ok": True,
            "subagent_id": row["id"],
            "status": row["status"],
            "result": row["result"],
            "error": row["error_message"],
            "cost_usd": row["cost_usd"],
            "task_preview": (row["task"] or "")[:100],
            "created_at": row["created_at"],
        }

    async def list_subagents(self, parent_session_key: str, status: str = "") -> list[dict[str, Any]]:
        rows = await self._repo().list_for_parent(
            parent_session_key, status=status, limit=20)
        return [
            {
                "id": row["id"],
                "status": row["status"],
                "task": row["task_preview"],
                "cost_usd": row["cost_usd"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def kill_subagent(self, subagent_id: str, *,
                            parent_session_key: str = "") -> dict[str, Any]:
        row = await self._repo().get(subagent_id)
        if row is None and len(subagent_id.strip()) >= 6:
            # The prompt renders 8-char task ids — accept a prefix, restricted
            # to detached_turn so a prefix can never reach another type's task.
            row = await self._repo().get_by_prefix(
                subagent_id.strip(), agent_type="detached_turn")
        if row is None:
            return {"ok": False, "error": "Subagent not found"}
        if parent_session_key and row["parent_session_key"] != parent_session_key:
            # Don't confirm the existence of another session's subagent.
            return {"ok": False, "error": "Subagent not found"}

        # Backburner detached turn: the live in-process task is cancelled via
        # the backburner registry; its supervisor does the terminal
        # bookkeeping (killed status, quiet goal settle, capture snapshot) on
        # observing the CancelledError. An already-finished task is an error
        # so the model relays the result instead of claiming a kill — the
        # likely "cancel it arrived just as the task finished" race.
        if row["agent_type"] == "detached_turn":
            if row["status"] != "running":
                return {"ok": False,
                        "error": f"Task already {row['status']} — nothing to cancel. "
                                 "Check its result instead (check_subagent)."}
            from server.services import backburner as _bb
            res = _bb.request_kill(row["id"])
            if not res.get("ok"):
                return {"ok": False,
                        "error": f"{res.get('error', 'not running')} — nothing to cancel. "
                                 "Check its result instead (check_subagent)."}
            logger.info("Subagent %s (detached_turn) kill requested", row["id"][:8])
            return {"ok": True, "subagent_id": row["id"], "status": "cancelling"}

        task = _running_tasks.pop(subagent_id, None)
        if task and not task.done():
            task.cancel()

        # For openai_voice subagents, tear down whatever they dispatched:
        # expire the linked browser voice_session (so the contact's link stops
        # working) and hang up any live phone call — the phone_calls row now
        # carries subagent_id (migration 355), so the call_sid is reachable.
        if row["agent_type"] == "openai_voice":
            from server.services.voice_dispatch_service import hangup_twilio_call

            try:
                now_iso = utcnow().isoformat()
                from server.services.voice_session_service import VoiceSessionService
                await VoiceSessionService.from_db(self.db).expire_for_subagent(
                    subagent_id, now_iso)
                # Keep the phone_calls mirror row in sync (calls UI).
                from server.repositories.phone_calls import PhoneCallRepository
                await PhoneCallRepository(self.db).cancel_voice_links_for_subagent(
                    subagent_id, now_iso)
            except Exception:
                logger.warning("Failed to expire voice_session for killed subagent %s", subagent_id[:8], exc_info=True)

            from server.repositories.phone_calls import PhoneCallRepository
            call = await PhoneCallRepository(self.db).latest_for_subagent(subagent_id)
            if call and call["status"] in ("active", "ringing"):
                if hangup_twilio_call(self._get_settings(), call["call_sid"]):
                    logger.info("Hung up phone call %s for killed subagent %s", call["call_sid"], subagent_id[:8])

        await self._update_status(subagent_id, "killed")
        try:
            from server.repositories.goals import GoalRepository
            goal = await GoalRepository(self.db).get_by_external_ref(subagent_id)
            if goal and goal["status"] == "active":
                from server.services.goal_service import settle_goal
                await settle_goal(self.ctx, goal["id"], status="cancelled",
                                  result="subagent killed", wake_origin=False)
        except Exception:
            logger.warning("failed to cancel goal for killed subagent %s",
                           subagent_id[:8], exc_info=True)
        logger.info("Subagent %s killed", subagent_id[:8])
        return {"ok": True, "subagent_id": subagent_id, "status": "killed"}

    async def cleanup_stale(self) -> int:
        """Set any running subagents to failed (e.g. after server restart).

        ``openai_voice`` subagents legitimately stay 'running' for
        minutes-to-hours while the contact hasn't picked up yet, so the
        immediate restart sweep skips them — but see ``fail_stale`` for the
        age horizons that eventually reap leaked voice rows and abandoned
        ``waiting_for_parent`` results.
        """
        now = utcnow().isoformat()
        count = await self._repo().fail_stale(now)
        if count:
            logger.info("Cleaned up %d stale subagents", count)
        return count

    # -- Internal helpers --

    async def _notify_parent(
        self, subagent_id: str, result_text: str, *, failed: bool = False,
    ) -> None:
        """Relay a subagent result to the parent conversation (Bob3 Phase V).

        First result settles the linked goal, whose completion wakes the
        origin conversation with the result — on any channel. Follow-up
        results (goal already settled) wake the parent directly.
        """
        row = await self._repo().get(subagent_id)
        if not row:
            return
        parent_session_key = row["parent_session_key"]

        short_id = subagent_id[:8]
        if row["agent_type"] == "script":
            content = (
                f"[Script {short_id}] {result_text}\n\n"
                f"This background script you started has finished. If it produced "
                f"an artifact the user asked for (image, document, file), send it "
                f"to them now with a short comment in your own voice. If it "
                f"failed, tell the user plainly and decide whether to retry."
            )
        else:
            content = (
                f"[Subagent {short_id}] {result_text}\n\n"
                f"Relay this result to the user with a summary. "
                f"You can also use message_subagent to reply or kill_subagent to terminate."
            )

        from server.repositories.goals import GoalRepository
        from server.services.goal_service import settle_goal
        from server.services.wake_service import wake_conversation

        settled = False
        try:
            goal = await GoalRepository(self.db).get_by_external_ref(subagent_id)
            if goal and goal["status"] == "active":
                settled = await settle_goal(
                    self.ctx, goal["id"],
                    status="failed" if failed else "completed",
                    result=content,
                )
        except Exception:
            logger.warning("failed to settle goal for subagent %s", short_id, exc_info=True)

        if not settled:
            try:
                await wake_conversation(
                    self.ctx, parent_session_key, content,
                    call_category="subagent_result",
                )
            except Exception:
                logger.exception("failed to wake parent %s for subagent %s",
                                 parent_session_key, short_id)

        if self.ctx.event_bus:
            await self.ctx.event_bus.publish("subagent.result_ready", {
                "subagent_id": subagent_id,
                "parent_session_key": parent_session_key,
                "result": result_text,
            })

    async def _update_status(
        self,
        subagent_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        now = utcnow().isoformat()
        await self._repo().set_status(subagent_id, status, now, error=error)
        await self._publish_event(subagent_id, status)

    async def _publish_event(self, subagent_id: str, status: str) -> None:
        if self.ctx.event_bus:
            await self.ctx.event_bus.publish("subagent.updated", {
                "subagent_id": subagent_id,
                "status": status,
            })

    async def _run_script(self, command: str) -> dict[str, Any]:
        """Run a shell command in the workspace as a background job (Bob3:
        async skill execution). Same sandbox and skill env as the bash tool;
        the parent conversation is woken with the output when it finishes."""
        from server.services.skill_env import build_skill_env
        from server.services.workspace_tools import _check_command_safety

        settings = self._get_settings()
        workspace = settings.harness.workspace_dir.expanduser().resolve()
        violation = _check_command_safety(
            command,
            db_path=settings.db_path,
            data_dir=settings.data_dir,
            config_dir=settings.config_dir,
        )
        if violation:
            raise RuntimeError(f"script blocked by sandbox: {violation}")

        # Quoting preflight (2026-09-05 crayon-portrait goal): a command the
        # shell can't even parse used to surface only as a failed background
        # goal minutes later. Reject up front — the parent wake then carries
        # the syntax error, and the retry can fix the quoting.
        from server.services.workspace_tools import bash_syntax_check
        syntax_error = await bash_syntax_check(command)
        if syntax_error:
            raise RuntimeError(
                f"script blocked by shell syntax: {syntax_error} — fix the "
                "quoting (backslash does not escape apostrophes in single "
                "quotes; use --prompt-file or a heredoc for prose)")

        venv_dir = settings.harness.venv_dir.expanduser()
        logger.info("script subagent: %s", command)
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=build_skill_env(workspace_dir=str(workspace), venv_dir=str(venv_dir)),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("script timed out after 900s")

        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        parts = [f"exit_code={proc.returncode}"]
        if out:
            parts.append(f"stdout:\n{out[-4000:]}")
        if err:
            parts.append(f"stderr:\n{err[-2000:]}")
        result_text = "\n".join(parts)
        if proc.returncode != 0:
            raise RuntimeError(f"script failed: {result_text}")
        return {"result": result_text, "cost_usd": 0.0, "session_id": ""}

    async def _run_local(
        self,
        *,
        session_key: str,
        persona: bool = False,
        model: str = "",
    ) -> dict[str, Any]:
        """Run a subagent in-process using the existing chat_with_tools loop."""
        settings = self._get_settings()
        resolved_model = model or settings.harness.local_subagent_model

        # Build system prompt
        if persona:
            from server.services.prompt_assembler import load_workspace_prompt
            system_content = await load_workspace_prompt(
                settings.harness.workspace_dir, db=self.db,
            )
        else:
            workspace_dir = settings.harness.workspace_dir.expanduser().resolve()
            system_content = (
                LOCAL_SUBAGENT_SYSTEM_PROMPT
                + f"\nYour workspace root is: {workspace_dir}"
            )

        # Build workspace-only tool set
        from server.services.workspace_tools import make_workspace_tools
        tools = make_workspace_tools(self.ctx, session_key=session_key)

        # Build messages from session history
        from server.services.prompt_assembler import build_chat_messages
        messages = await build_chat_messages(
            None, session_key,
            db=self.db,
            system_content=system_content,
            max_history=50,
        )

        # Dispatch via LLM dispatch (logs calls, publishes events)
        from server.services.llm_dispatch import LLMDispatchService
        result_text = await LLMDispatchService(self.ctx).chat_with_tools(
            messages=messages,
            tools=tools,
            model=resolved_model,
            max_iterations=30,
            call_category="local_subagent",
            session_key=session_key,
        )

        logger.info(
            "Local subagent: model=%s chars=%d",
            resolved_model, len(result_text),
        )

        return {"result": result_text, "session_id": "", "cost_usd": 0}

    async def _run_claude(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        model: str | None = None,
        max_budget: float = 5.0,
    ) -> dict[str, Any]:
        """Run Claude Code as a subprocess and return JSON output."""
        claude_bin = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
        if not Path(claude_bin).is_file():
            raise RuntimeError(f"claude CLI not found (tried PATH and {claude_bin})")

        cmd = [
            claude_bin, "-p",
            "--output-format", "json",
            "--max-budget-usd", str(max_budget),
            "--allowed-tools", "Read Write Glob Grep Edit Bash",
            "--system-prompt", SUBAGENT_SYSTEM_PROMPT,
        ]
        if model:
            cmd.extend(["--model", model])
        if session_id:
            cmd.extend(["--resume", session_id])
        else:
            session_id = str(uuid4())
            cmd.extend(["--session-id", session_id])
        cmd.append(prompt)

        logger.info("Spawning Claude Code: session=%s cwd=%s", session_id, cwd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        timeout_s = self._get_settings().harness.skill_dev_timeout_seconds
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            # Kill the run: an un-killed process keeps mutating the workspace
            # after the failure is recorded, and its piped output is lost anyway.
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"Claude Code timed out after {timeout_s:.0f}s "
                f"(resumable via: claude --resume {session_id})"
            ) from None

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            logger.error("Claude Code failed (rc=%d): %s", proc.returncode, err)
            raise RuntimeError(f"Claude Code exited with code {proc.returncode}: {err[:500]}")

        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            raise RuntimeError("Claude Code returned empty output")

        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {"result": output, "session_id": "", "cost_usd": 0}


def _register_spawn_executor() -> None:
    """SpawnSubagent effect executor (Bob3 Phase VI item 5): claude/local are
    executor kinds on the durable spawn record. Starts the background run and
    returns immediately; guards against re-spawning on pump re-delivery."""
    from server.services import effects as effects_svc

    async def _exec_spawn(ctx, payload):
        subagent_id = payload["subagent_id"]
        from server.repositories.subagents import SubagentRepository
        sub_status = await SubagentRepository(ctx.db).status_of(subagent_id)
        if sub_status is None:
            raise RuntimeError(f"subagent {subagent_id} not found")
        if sub_status not in ("created", "running"):
            return subagent_id  # already finished — idempotent re-delivery
        existing = _running_tasks.get(subagent_id)
        if existing is not None and not existing.done():
            return subagent_id  # run already in flight
        svc = SubagentService(ctx)
        t = asyncio.create_task(svc._run_subagent(subagent_id, payload.get("task", "")))
        _running_tasks[subagent_id] = t
        return subagent_id

    effects_svc.register_executor("subagent_spawn", _exec_spawn)


_register_spawn_executor()
