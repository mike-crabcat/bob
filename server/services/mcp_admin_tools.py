"""Agent-side MCP administration: register/attach at a trusted contact's
request (2026-09-14, Mike's call).

Trust model — "only at the request of trusted contacts":
- The tools are attached ONLY in human-initiated turns from a trusted
  contact (the structural gate, steering-style: an untrusted session never
  sees them, so an untrusted requester can't even trigger a request).
- The owner (contacts.is_default) registers directly.
- Any other trusted contact's request parks an approval in the owner's DM
  via the approvals machinery (kind=approval_request, type=mcp_server);
  approving executes the registration deterministically from the stored
  proposal and wakes the requesting conversation. This also covers
  prompt-injection into a trusted session: every non-owner registration
  gets the owner's eyes before any command runs.

Servers registered through this path attach to the requesting conversation
only — is_global stays an owner decision via the dashboard API/CLI.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from server.services.tools import Tool

logger = logging.getLogger(__name__)

APPROVAL_TYPE = "mcp_server"


async def make_mcp_admin_tools(
    ctx: Any, *, session_key: str, is_trusted: bool,
    contact_id: str | None, human_initiated: bool,
) -> list[Tool]:
    """MCP admin tools for trusted human-initiated turns only. Returns []
    anywhere else — absence IS the untrusted-requester gate."""
    if not (is_trusted and contact_id and human_initiated):
        return []
    if not ctx.settings.mcp.enabled:
        return []

    async def _requester_label() -> str:
        from server.repositories.contacts import ContactRepository

        contact = await ContactRepository(ctx.db).get(contact_id)
        return (contact or {}).get("name") or session_key

    async def _list_mcp_servers() -> str:
        from server.repositories.mcp import McpServerRepository

        rows = await McpServerRepository(ctx.db).list()
        if not rows:
            return ("No MCP servers are registered. I can register one at "
                    "your request with register_mcp_server.")
        manager = getattr(ctx, "mcp", None)
        status = {s["server_id"]: s for s in (manager.status() if manager else [])}
        lines = []
        for r in rows:
            s = status.get(r["id"], {})
            # Count AND note — never either/or: "0 tools" with the reason
            # hidden is how the elevenlabs budget bug went unnoticed.
            state = f"{s.get('tool_count', 0)} tools"
            if s.get("error"):
                state += f" — {s['error']}"
            env_keys = ",".join(json.loads(r["env_json"] or "{}"))
            lines.append(
                f"- {r['name']} [{r['transport']}] {state}"
                f"{' global' if r['is_global'] else ''}"
                f"{' trusted-only' if r['trusted_only'] else ''}"
                f"{' DISABLED' if not r['enabled'] else ''}"
                + (f" env keys: {env_keys}" if env_keys else ""))
        return "MCP servers:\n" + "\n".join(lines)

    async def _register_mcp_server(**kwargs: Any) -> str:
        from server.repositories.contacts import ContactRepository
        from server.repositories.mcp import McpServerRepository
        from server.services.steering import owner_contact, owner_dm_session_key

        body = {k: kwargs.get(k) for k in (
            "name", "transport", "command", "args", "env", "url", "headers",
            "note") if kwargs.get(k) is not None}
        fields, error = _validated(fields_body=body)
        if error or fields is None:
            return f"Error: {error or 'invalid registration fields'}"

        repo = McpServerRepository(ctx.db)
        if await repo.get_by_name(fields["name"]):
            return (f"Error: an MCP server named '{fields['name']}' "
                    f"already exists")
        if len(await repo.list()) >= ctx.settings.mcp.max_servers:
            return (f"Error: MCP server cap reached "
                    f"({ctx.settings.mcp.max_servers}) — the operator needs "
                    f"to prune before adding more")

        contact = await ContactRepository(ctx.db).get(contact_id)
        requester_name = (contact or {}).get("name") or session_key
        owner = await owner_contact(ctx)
        if owner is None:
            return ("Error: no default contact configured — the operator "
                    "must set one before MCP registration requests work")

        # Owner bypass: self-approval is pure friction.
        if str(contact_id) == str(owner["id"]):
            row = await repo.create(**fields, created_by=requester_name)
            await _attach_here(ctx, session_key, row["id"], requester_name)
            await _refresh(ctx, row)
            return (f"Registered MCP server '{row['name']}' and attached it "
                    f"to this conversation. Its tools appear as "
                    f"mcp_{row['name']}_* once the tool cache refreshes "
                    f"(~a minute); check with list_mcp_servers or "
                    f"test_mcp_server.")

        owner_dm = await owner_dm_session_key(ctx, owner)
        if owner_dm is None:
            return ("Error: the owner has no active DM binding — cannot "
                    "route the approval")

        # Dedupe: a repeated attempt must not mint a second pending approval.
        from server.repositories.approvals import ApprovalRepository

        for pending in await ApprovalRepository(ctx.db).pending_of_type(
                APPROVAL_TYPE, entity_id=_entity_id(fields["name"])):
            return (f"An approval for MCP server '{fields['name']}' is "
                    f"already waiting for the owner (id {pending['id']}).")

        proposal = {
            "summary": "\n".join([
                f"MCP server: {fields['name']} ({fields['transport']})",
                f"Requested by: {requester_name}",
                "",
                _describe(fields),
                "",
                "Approving registers the server and attaches it to the "
                "requesting conversation. stdio commands run as the bob "
                "user — this is a code-execution trust decision.",
            ]),
            "fields": fields,
            "created_by": requester_name,
            "origin_session_key": session_key,
            "requester_label": requester_name,
        }
        from server.services.effects import emit_and_deliver

        result = await emit_and_deliver(
            ctx, kind="approval_request",
            idempotency_key=f"approval_request:{uuid4()}",
            payload={
                "approval_type": APPROVAL_TYPE,
                "entity_id": _entity_id(fields["name"]),
                "title": f"Register MCP server '{fields['name']}'",
                "description": f"MCP registration request from {requester_name}",
                "proposal": proposal,
                "requested_by": session_key,
                "origin_conversation_id": owner_dm,
            })
        if not result.get("ok"):
            return f"Error: {result.get('error', 'approval request failed')}"
        return (f"Approval requested: {owner.get('name') or 'the owner'} "
                f"needs to approve MCP server '{fields['name']}' in their "
                f"DM. Once approved it registers automatically and joins "
                f"this conversation — I'll confirm then.")

    async def _attach_mcp_server(server_name: str = "", **_: Any) -> str:
        from server.repositories.mcp import McpServerRepository

        repo = McpServerRepository(ctx.db)
        row = await repo.get_by_name((server_name or "").strip())
        if row is None:
            return f"Error: no MCP server named '{server_name}'"
        if not row["enabled"]:
            return f"Error: MCP server '{server_name}' is disabled"
        await _attach_here(ctx, session_key, row["id"],
                           await _requester_label())
        return (f"Attached MCP server '{row['name']}' to this conversation. "
                f"Tools: mcp_{row['name']}_*.")

    async def _detach_mcp_servers(**_: Any) -> str:
        from server.repositories.conversations import ConversationRepository
        from server.repositories.mcp import McpServerRepository

        cid = await ConversationRepository(ctx.db).resolve_cid(session_key)
        repo = McpServerRepository(ctx.db)
        current = await repo.attachments_for(cid)
        if not current:
            return "No MCP servers are attached to this conversation."
        await repo.set_attachments(cid, [], attached_by=await _requester_label())
        names = ", ".join(a["server_name"] for a in current)
        return f"Detached MCP servers from this conversation: {names}."

    async def _test_mcp_server(server_name: str = "", **_: Any) -> str:
        from server.repositories.mcp import McpServerRepository

        manager = getattr(ctx, "mcp", None)
        if manager is None:
            return "Error: MCP manager is not running"
        row = await McpServerRepository(ctx.db).get_by_name(
            (server_name or "").strip())
        if row is None:
            return f"Error: no MCP server named '{server_name}'"
        result = await manager.test_server(row)
        if result.get("ok"):
            tools = ", ".join(result.get("tools") or []) or "(none)"
            return f"MCP server '{server_name}' is reachable. Tools: {tools}"
        return (f"MCP server '{server_name}' failed its health check: "
                f"{result.get('error')}")

    async def _set_mcp_server_scope(server_name: str = "",
                                    scope: str = "conversation",
                                    **_: Any) -> str:
        from server.repositories.mcp import McpServerRepository
        from server.services.steering import owner_contact

        # Global exposure is the operator's call by design (2026-09-14);
        # the owner asking in chat IS that call (2026-09-16). Everyone
        # else — trusted or not — gets a hard no, not an approval flow:
        # widening a server to every conversation is not theirs to ask.
        owner = await owner_contact(ctx)
        if owner is None or str(contact_id) != str(owner["id"]):
            return ("Error: making an MCP server globally available is an "
                    "operator-only decision — ask the operator to flip it "
                    "(dashboard or `bob mcp update <name> --global`)")

        normalized = (scope or "").strip().lower()
        if normalized in ("global", "everywhere", "all"):
            is_global = True
        elif normalized in ("conversation", "chat", "attached", "local"):
            is_global = False
        else:
            return ("Error: scope must be 'global' (every conversation) or "
                    f"'conversation' (attached only), got {scope!r}")

        repo = McpServerRepository(ctx.db)
        row = await repo.get_by_name((server_name or "").strip())
        if row is None:
            return f"Error: no MCP server named '{server_name}'"
        await repo.update(row["id"], is_global=is_global)
        if is_global:
            return (f"MCP server '{row['name']}' is now globally available — "
                    f"every conversation gets its tools "
                    f"(mcp_{row['name']}_*).")
        return (f"MCP server '{row['name']}' is now scoped to attached "
                f"conversations only.")

    enum = {"type": "string", "enum": ["stdio", "http"]}
    return [
        Tool(
            name="list_mcp_servers",
            description=(
                "List registered MCP tool servers with their status. "
                "Secrets are never shown."),
            parameters={}, required=[],
            handler=_list_mcp_servers),
        Tool(
            name="register_mcp_server",
            description=(
                "Register an external MCP tool server at a trusted "
                "contact's request and attach it to this conversation. "
                "Owner requests take effect immediately; other trusted "
                "contacts' requests go to the owner for approval first. "
                "Servers registered here are conversation-scoped — making "
                "one globally available is the operator's dashboard call. "
                "stdio commands run as the bob user, so only register "
                "servers from sources the requester actually trusts."),
            parameters={
                "name": {"type": "string",
                         "description": "Server slug: lowercase letters, "
                                        "digits, - and _ (tools appear as "
                                        "mcp_<name>_<tool>)"},
                "transport": enum,
                "command": {"type": "string",
                            "description": "stdio only: absolute path to the "
                                           "executable (e.g. /usr/bin/npx)"},
                "args": {"type": "array", "items": {"type": "string"},
                         "description": "stdio only: command arguments "
                                        "(e.g. ['-y', '@mcp/fetch'])"},
                "env": {"type": "object",
                        "description": "stdio servers ONLY: environment "
                                       "variables for the server process. "
                                       "Ignored for http servers — http "
                                       "auth goes in headers. Values may "
                                       "reference service env vars as "
                                       "${VAR_NAME} (expanded at launch; "
                                       "store the reference, not the "
                                       "secret)"},
                "url": {"type": "string",
                        "description": "http only: server URL "
                                       "(https://...)"},
                "headers": {"type": "object",
                            "description": "http servers ONLY: request "
                                           "headers, e.g. "
                                           "{\"Authorization\": \"Bearer "
                                           "${SOME_API_KEY}\"} — ${VAR_NAME} "
                                           "references expand from the "
                                           "service environment at connect "
                                           "time, so the key never needs to "
                                           "be typed literally"},
                "note": {"type": "string",
                         "description": "What this server is for, and who "
                                        "asked for it"},
            },
            required=["name", "transport"],
            handler=_register_mcp_server),
        Tool(
            name="attach_mcp_server",
            description=(
                "Attach an already-registered MCP server to this "
                "conversation."),
            parameters={
                "server_name": {"type": "string",
                                "description": "Name of the registered server"},
            },
            required=["server_name"],
            handler=_attach_mcp_server),
        Tool(
            name="detach_mcp_servers",
            description=(
                "Detach all MCP servers from this conversation (the "
                "servers themselves stay registered)."),
            parameters={}, required=[],
            handler=_detach_mcp_servers),
        Tool(
            name="test_mcp_server",
            description=(
                "Health-check an MCP server with a fresh connection and "
                "list the tools it exposes."),
            parameters={
                "server_name": {"type": "string",
                                "description": "Name of the registered server"},
            },
            required=["server_name"],
            handler=_test_mcp_server),
        Tool(
            name="set_mcp_server_scope",
            description=(
                "OPERATOR ONLY: make an MCP server's tools available in "
                "every conversation (scope='global') or only in "
                "conversations it's attached to (scope='conversation'). "
                "When the owner asks in chat to make a server global, this "
                "is the tool for it; anyone else gets refused."),
            parameters={
                "server_name": {"type": "string",
                                "description": "Name of the registered server"},
                "scope": {"type": "string",
                          "enum": ["global", "conversation"],
                          "description": "'global' = every conversation; "
                                         "'conversation' = attached only"},
            },
            required=["server_name", "scope"],
            handler=_set_mcp_server_scope),
    ]


# ── helpers ───────────────────────────────────────────────────────────

def _entity_id(name: str) -> str:
    return f"mcp_server:{name}"


def _validated(*, fields_body: dict) -> tuple[dict | None, str]:
    from server.services.mcp_service import validate_mcp_server_fields

    fields, error = validate_mcp_server_fields(fields_body)
    if error:
        return None, error
    # This path never registers globally — strip the flag if a model
    # invented it (defensive enum handling, house rule).
    fields.pop("is_global", None)
    return fields, ""


def _describe(fields: dict) -> str:
    lines = []
    if fields.get("transport") == "stdio":
        args = " ".join(fields.get("args") or [])
        lines.append(f"Command: {fields.get('command', '')} {args}".rstrip())
        env = fields.get("env") or {}
        if env:
            lines.append("Env keys: " + ", ".join(env))
    else:
        lines.append(f"URL: {fields.get('url', '')}")
        headers = fields.get("headers") or {}
        if headers:
            lines.append("Header keys: " + ", ".join(headers))
    if fields.get("note"):
        lines.append(f"Note: {fields['note']}")
    return "\n".join(lines)


async def _attach_here(ctx: Any, session_key: str, server_id: str,
                       attached_by: str = "") -> None:
    """Extend (not replace) this conversation's attachments."""
    from server.repositories.conversations import ConversationRepository
    from server.repositories.mcp import McpServerRepository

    cid = await ConversationRepository(ctx.db).resolve_cid(session_key)
    repo = McpServerRepository(ctx.db)
    current = [a["mcp_server_id"]
               for a in await repo.attachments_for(cid)]
    if server_id not in current:
        current.append(server_id)
    await repo.set_attachments(cid, current, attached_by=attached_by)


async def _refresh(ctx: Any, row: dict) -> None:
    manager = getattr(ctx, "mcp", None)
    if manager is not None:
        try:
            await manager.refresh_server(row)
        except Exception:
            logger.exception("mcp refresh after registration failed for %s",
                             row.get("name"))


# ── approval follow-through ───────────────────────────────────────────

async def on_approved(ctx: Any, row: dict[str, Any]) -> None:
    """approval_tools on-approved hook: register exactly what was approved,
    from the stored proposal. Never raises — the decision is already
    durably recorded; a crash here must not dead-letter the respond."""
    try:
        proposal = json.loads(row.get("proposal_data") or "{}")
    except (TypeError, ValueError):
        logger.error("mcp approval %s has unparseable proposal; skipped",
                     row.get("id"))
        return
    fields, error = _validated(fields_body=proposal.get("fields") or {})
    if error or fields is None:
        logger.error("mcp approval %s proposal failed re-validation (%s); "
                     "skipped", row.get("id"), error)
        return

    from server.repositories.mcp import McpServerRepository

    repo = McpServerRepository(ctx.db)
    if await repo.get_by_name(fields["name"]):
        logger.info("mcp approval %s: server '%s' already exists; skipping "
                    "duplicate registration", row.get("id"), fields["name"])
        return
    if len(await repo.list()) >= ctx.settings.mcp.max_servers:
        logger.error("mcp approval %s: server cap reached; skipping",
                     row.get("id"))
        return

    origin = proposal.get("origin_session_key") or ""
    created = await repo.create(**fields,
                                created_by=proposal.get("created_by", ""))
    if origin:
        try:
            await _attach_here(ctx, origin, created["id"],
                               proposal.get("created_by", ""))
        except Exception:
            logger.exception("mcp approval %s: attach to origin failed",
                             row.get("id"))
    await _refresh(ctx, created)

    # Tell the requesting conversation it's live (outbox-retried, keyed to
    # this approval so a redelivered respond can never double-wake).
    if origin:
        from server.services.effects import emit_and_deliver

        content = (
            f"MCP server '{created['name']}' was approved and is now "
            f"registered, attached to this conversation. Its tools appear "
            f"as mcp_{created['name']}_* (list_mcp_servers to check).")
        try:
            await emit_and_deliver(
                ctx, kind="mcp_registered",
                idempotency_key=f"mcp_registered:{row.get('id')}",
                payload={"target_key": origin, "content": content})
        except Exception:
            logger.exception("mcp approval %s: notify wake emit failed",
                             row.get("id"))


def register() -> None:
    """Bind the mcp_registered executor and the on-approved hook
    (idempotent; approval_tools' registration tail re-runs it so the pump
    can never deliver an approval_respond with the hook missing)."""
    from server.services import approval_tools
    from server.services import effects as effects_svc

    async def _exec_registered(ctx, payload):
        from server.services.wake_service import wake_conversation

        await wake_conversation(
            ctx, payload["target_key"], payload["content"],
            call_category="mcp_registered",
            metadata={"kind": "mcp_registered"},
            provenance="steer")
        return None

    effects_svc.register_executor("mcp_registered", _exec_registered,
                                  retryable=True)
    approval_tools.register_on_approved(APPROVAL_TYPE, on_approved)


register()
