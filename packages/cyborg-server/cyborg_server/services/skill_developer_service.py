"""Orchestrate skill creation by delegating to Claude Code via subprocess."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any
from uuid import uuid4

from cyborg_server.context import AppContext
from cyborg_server.services.base import BaseService, utcnow

logger = logging.getLogger(__name__)

SKILL_DEV_SYSTEM_PROMPT = """\
You are a skill developer for an AI agent called Cyborg.
Your job is to create skills as markdown definitions with optional scripts.

SKILL FORMAT:
- Each skill lives in skills/{skill_name}/skill.md
- skill.md has YAML frontmatter and a body with instructions:

```
---
name: skill_name
description: What this skill does
trigger: when to activate this skill
---

## Instructions
Step-by-step instructions for the agent to follow when this skill activates.
Reference tools Cyborg has: list_files, read_file, write_file, update_agenda,
send_whatsapp_message, search_contacts, send_whatsapp_to_contact,
email_reply, email_skip.
```

- Optional Python scripts alongside skill.md (e.g. skills/{name}/helper.py)
- Instructions should be clear and actionable
- Keep skills focused on a single capability

PROCESS:
1. Read existing skills to understand patterns
2. Plan what skill to create (what files, what content)
3. For PLANNING: describe your plan but do NOT create files yet
4. For IMPLEMENTATION: create the skill files using Read/Write tools
"""


class SkillDeveloperService(BaseService):
    """Manages skill creation delegations to Claude Code."""

    async def plan_skill(self, user_story: str, session_key: str) -> dict[str, Any]:
        """Send a user story to Claude Code for planning. Returns plan text."""
        delegation_id = str(uuid4())
        now = utcnow().isoformat()

        await self.db.execute(
            """INSERT INTO skill_delegations
               (id, session_key, user_story, status, created_at, updated_at)
               VALUES (?, ?, ?, 'planning', ?, ?)""",
            (delegation_id, session_key, user_story, now, now),
        )

        settings = self._get_settings()
        workspace_dir = settings.harness.workspace_dir.expanduser().resolve()
        skills_dir = workspace_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        prompt = (
            f"USER STORY:\n{user_story}\n\n"
            "Create a skill for this. First, read any existing skills to understand patterns, "
            "then describe your plan for what skill to create (name, files, approach). "
            "Do NOT create any files yet — just plan."
        )

        try:
            result = await self._run_claude(
                prompt,
                cwd=skills_dir,
                model=settings.harness.skill_dev_model,
                max_budget=settings.harness.skill_dev_max_budget_usd,
            )
        except Exception as e:
            await self._update_status(delegation_id, "failed", error=str(e))
            return {"ok": False, "error": str(e), "delegation_id": delegation_id}

        claude_session_id = result.get("session_id", "")
        plan_text = result.get("result", "")
        cost = result.get("cost_usd", 0)

        await self.db.execute(
            """UPDATE skill_delegations
               SET plan = ?, claude_session_id = ?, status = 'plan_ready',
                   cost_usd = ?, updated_at = ?
               WHERE id = ?""",
            (plan_text, claude_session_id, cost, utcnow().isoformat(), delegation_id),
        )

        logger.info(
            "Skill plan ready: delegation=%s session=%s cost=%.4f",
            delegation_id, claude_session_id, cost,
        )

        return {
            "ok": True,
            "delegation_id": delegation_id,
            "plan": plan_text,
            "cost_usd": cost,
        }

    async def implement_skill(self, delegation_id: str) -> dict[str, Any]:
        """Resume Claude Code session and implement the approved plan."""
        row = await self.db.fetch_one(
            "SELECT * FROM skill_delegations WHERE id = ?",
            (delegation_id,),
        )
        if row is None:
            return {"ok": False, "error": "Delegation not found"}
        if row["status"] != "plan_ready":
            return {"ok": False, "error": f"Delegation is in status '{row['status']}', expected 'plan_ready'"}

        claude_session_id = row["claude_session_id"]
        if not claude_session_id:
            return {"ok": False, "error": "No Claude session ID to resume"}

        settings = self._get_settings()
        workspace_dir = settings.harness.workspace_dir.expanduser().resolve()
        skills_dir = workspace_dir / "skills"

        await self._update_status(delegation_id, "implementing")

        try:
            result = await self._run_claude(
                "Plan approved. Implement it now — create the skill files.",
                cwd=skills_dir,
                session_id=claude_session_id,
                model=settings.harness.skill_dev_model,
                max_budget=settings.harness.skill_dev_max_budget_usd,
            )
        except Exception as e:
            await self._update_status(delegation_id, "failed", error=str(e))
            return {"ok": False, "error": str(e), "delegation_id": delegation_id}

        result_text = result.get("result", "")
        cost = result.get("cost_usd", 0)
        total_cost = (row["cost_usd"] or 0) + cost

        # Discover what files were created
        files_created = await self._find_new_skills(skills_dir)

        await self.db.execute(
            """UPDATE skill_delegations
               SET status = 'completed', result_summary = ?,
                   files_created_json = ?, cost_usd = ?, updated_at = ?
               WHERE id = ?""",
            (
                result_text,
                json.dumps(files_created),
                total_cost,
                utcnow().isoformat(),
                delegation_id,
            ),
        )

        # Clear skill loader cache so new skills appear
        from cyborg_server.services.skill_loader import _skills_cache
        import cyborg_server.services.skill_loader as sl
        sl._skills_cache = None

        logger.info(
            "Skill implemented: delegation=%s files=%s cost=%.4f",
            delegation_id, files_created, total_cost,
        )

        return {
            "ok": True,
            "delegation_id": delegation_id,
            "result": result_text,
            "files_created": files_created,
            "cost_usd": total_cost,
        }

    async def reject_skill(self, delegation_id: str, reason: str) -> dict[str, Any]:
        """Reject a delegation plan."""
        row = await self.db.fetch_one(
            "SELECT status FROM skill_delegations WHERE id = ?",
            (delegation_id,),
        )
        if row is None:
            return {"ok": False, "error": "Delegation not found"}

        await self._update_status(delegation_id, "rejected", error=reason)
        return {"ok": True, "delegation_id": delegation_id, "status": "rejected"}

    async def get_status(self, delegation_id: str) -> dict[str, Any]:
        """Get delegation status."""
        row = await self.db.fetch_one(
            "SELECT * FROM skill_delegations WHERE id = ?",
            (delegation_id,),
        )
        if row is None:
            return {"ok": False, "error": "Delegation not found"}
        return {"ok": True, "delegation_id": delegation_id, "status": row["status"]}

    async def _run_claude(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str | None = None,
        model: str = "sonnet",
        max_budget: float = 5.0,
    ) -> dict[str, Any]:
        """Run Claude Code as a subprocess and return JSON output."""
        claude_bin = shutil.which("claude")
        if not claude_bin:
            raise RuntimeError("claude CLI not found in PATH")

        cmd = [
            claude_bin, "-p",
            "--output-format", "json",
            "--model", model,
            "--max-budget-usd", str(max_budget),
            "--allowed-tools", "Read Write Glob Grep",
            "--system-prompt", SKILL_DEV_SYSTEM_PROMPT,
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(prompt)

        logger.info("Spawning Claude Code: session=%s cwd=%s", session_id, cwd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=self._get_settings().harness.skill_dev_timeout_seconds,
        )

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
            # Fallback: treat raw text as result
            return {"result": output, "session_id": "", "cost_usd": 0}

    async def _update_status(
        self,
        delegation_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        now = utcnow().isoformat()
        if error:
            await self.db.execute(
                "UPDATE skill_delegations SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                (status, error, now, delegation_id),
            )
        else:
            await self.db.execute(
                "UPDATE skill_delegations SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, delegation_id),
            )

    async def _find_new_skills(self, skills_dir: Path) -> list[str]:
        """List skill directories that contain a skill.md."""
        files = []
        if not skills_dir.is_dir():
            return files
        for child in sorted(skills_dir.iterdir()):
            if child.is_dir() and (child / "skill.md").is_file():
                files.append(child.name)
        return files
