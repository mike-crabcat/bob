"""The ONE interception boundary (docs/evals.md → Interception).

Everything inside a world runs production code against the world's
fixture DB and workspace: the real inbound handler, DispatchRunner,
prompt assembly, tools, goal/approval effects, memory writes. This
module is the only place the eval process diverges from production, and
it diverges only where Bob would reach OUTSIDE the box:

- WhatsApp transport — FakeBridge overrides the websocket methods (send,
  media, reaction, ack, typing, presence); the bridge's own effect
  executors call them, so sends still ride the effects outbox.
- External effect kinds — email_reply / email_send / subagent_spawn
  executors record instead of delivering/spawning. Internal kinds
  (goal_*, approval_*, mcp_registered) keep their real executors: they
  ARE state writes, and the fixture DB is where state lives.
- Process supervision — bg job spawn / stop / unit state / liveness are
  simulated (the real layer shells out to systemctl on THIS host: an eval
  model calling bg_stop("radio") would stop a live unit). _run_cmd is
  hard-blocked as a backstop.
- Voice placement (Twilio), Home Assistant reads, MCP servers (worlds
  declare fixture servers; FixtureMcpManager answers their calls).
- The attention window — submit() dispatches immediately. Tier-2 probing
  has its own probe matrix; here a turn is what's under test.
- bash — the real tool runs in the world workspace; a guard refuses
  commands that name the host (/home/bob, ~, $HOME, systemctl, kill,
  the local API) — production's sandbox filter is pointed at the world's
  data/config dirs, so the host's would otherwise be reachable.

Observation (not interception) also lives here: LLMDispatchService.run_turn
is wrapped so every turn in a world — whatever channel produced it — is
recorded with its messages, tool calls and result, and the model pin /
effort override applies to Bob's own turns.

Worlds are routed by a ContextVar: tasks inherit it, so a world's
background work (backburner flights, wake dispatches) records into the
right world even when cases run concurrently.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from server.evals.world import World

logger = logging.getLogger(__name__)

CURRENT_WORLD: contextvars.ContextVar["World | None"] = contextvars.ContextVar(
    "bob_eval_world", default=None)

# Background passes keep their configured model unless the world opts in
# (extraction cases pin it: extraction quality is what they measure).
BACKGROUND_CATEGORIES = frozenset({
    "memory_silent_turn", "memory_reconciliation", "memory_merge_confirmation",
    "attention_probe", "detach_probe", "claim_router_probe", "goal_revise",
    "docs_search", "reflection", "call_summary", "voice_session_summary",
    "voice_session_report", "dream_synthesis", "dream_review",
    "dream_prospective", "dream_factcheck", "dream_announce", "eval_judge",
})

_BASH_HOST_GUARD = re.compile(
    r"/home/bob|(?<![\w.])~(?=/|\s|$)|\$HOME|\$\{HOME\}|\bsystemctl\b|"
    r"\bjournalctl\b|\bpkill\b|\bkillall\b|(?<![\w-])kill\s|"
    r"\bbob\.service\b|(?:127\.0\.0\.1|localhost):84\d\d")

_installed = False


def world() -> "World | None":
    return CURRENT_WORLD.get()


# --------------------------------------------------------------------------
# observation: the run_turn probe
# --------------------------------------------------------------------------

def _wrap_tool(w: "World", tool: Any, turn: Any) -> Any:
    inner = tool.handler
    name = tool.name

    async def handler(*args: Any, **kwargs: Any) -> Any:
        if name == "bash":
            cmd = str(kwargs.get("command") or (args[0] if args else ""))
            # Production paths in skill docs point at the live workspace;
            # in a world they mean the world's workspace.
            ws = str(w.workspace)
            cmd = (cmd.replace("/home/bob/workspace", ws)
                   .replace("~/workspace", ws).replace("$HOME/workspace", ws))
            if args:
                args = (cmd, *args[1:])
            else:
                kwargs = {**kwargs, "command": cmd}
            if _BASH_HOST_GUARD.search(cmd):
                out = ("BLOCKED: path outside the workspace. The workspace is "
                       "the cwd — use relative paths.")
                turn.record_tool(name, kwargs, out, guarded=True)
                return out
        elif name in ("read_file", "edit_file", "multi_edit", "read_image",
                      "read_video"):
            # Same live→world path mapping as bash; the tools' own resolver
            # then confines them to the world workspace.
            ws = str(w.workspace)
            raw = str(kwargs.get("path") or (args[0] if args else ""))
            path = (raw.replace("/home/bob/workspace", ws)
                    .replace("~/workspace", ws).replace("$HOME/workspace", ws))
            if args:
                args = (path, *args[1:])
            else:
                kwargs = {**kwargs, "path": path}
        t0 = time.monotonic()
        try:
            out = await inner(*args, **kwargs)
        except Exception as exc:
            turn.record_tool(name, kwargs, f"[raised] {exc!r}",
                             seconds=time.monotonic() - t0)
            raise
        turn.record_tool(name, kwargs, out, seconds=time.monotonic() - t0)
        return out

    return dataclasses.replace(tool, handler=handler)


# The world turn the current LLM call belongs to — lets the OpenAIService
# layer record server-side (hosted) tool use, which never passes a handler.
_CURRENT_TURN: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "bob_eval_current_turn", default=None)


def _install_hosted_tool_probe() -> None:
    """OpenAI-native web_search runs inside the model call; run_turn reports
    it through on_tool_call with a 'hosted …' summary. Record it on the
    world turn so tool checks see it (2026-10-11: luna searched natively and
    failed 'web_search was called' checks 0/18)."""
    from server.services.openai_service import OpenAIService

    original = OpenAIService.run_turn
    if getattr(original, "_bob_eval_hosted", False):
        return

    async def run_turn(self, *args, **kwargs):
        turn = _CURRENT_TURN.get()
        inner_cb = kwargs.get("on_tool_call")
        if turn is not None:
            async def cb(name, tool_args, summary):
                if str(summary).startswith("hosted "):
                    turn.record_tool(name, tool_args, summary)
                if inner_cb is not None:
                    await inner_cb(name, tool_args, summary)
            kwargs["on_tool_call"] = cb
        return await original(self, *args, **kwargs)

    run_turn._bob_eval_hosted = True  # type: ignore[attr-defined]
    OpenAIService.run_turn = run_turn  # type: ignore[method-assign]


def _install_probe() -> None:
    from server.services.llm_dispatch import LLMDispatchService

    original = LLMDispatchService.run_turn
    if getattr(original, "_bob_eval_probe", False):
        return

    async def run_turn(self, messages, tools, **kwargs):
        w = world()
        if w is None:
            return await original(self, messages, tools, **kwargs)
        category = kwargs.get("call_category", "tool_call")
        background = category in BACKGROUND_CATEGORIES
        if w.pin_model and (not background or w.pin_background):
            kwargs["model"] = w.pin_model
        if w.effort and not background:
            kwargs["reasoning_effort"] = w.effort
        turn = w.begin_turn(
            session_key=kwargs.get("session_key") or "",
            call_category=category, dispatch_id=kwargs.get("dispatch_id"),
            messages=messages, tools=[t.name for t in tools],
            background=background)
        wrapped = [_wrap_tool(w, t, turn) for t in tools]
        token = _CURRENT_TURN.set(turn)
        try:
            result = await original(self, messages, wrapped, **kwargs)
        except BaseException as exc:
            turn.finish(error=exc)
            raise
        finally:
            _CURRENT_TURN.reset(token)
        turn.finish(result=result)
        return result

    run_turn._bob_eval_probe = True  # type: ignore[attr-defined]
    LLMDispatchService.run_turn = run_turn  # type: ignore[method-assign]


# --------------------------------------------------------------------------
# WhatsApp transport
# --------------------------------------------------------------------------

def make_fake_bridge(ctx: Any) -> Any:
    from server.services.whatsapp_bridge_service import WhatsAppBridgeService

    class FakeBridge(WhatsAppBridgeService):
        """Production bridge service minus the websocket."""

        async def send_message(self, chat_id: str, text: str, *,
                               reply_to: str | None = None) -> str:
            return _record_delivery("whatsapp_send", {
                "chat_id": chat_id, "text": text, "reply_to": reply_to})

        async def send_media(self, chat_id: str, file_path: str, *,
                             caption: str = "") -> str:
            return _record_delivery("whatsapp_send_media", {
                "chat_id": chat_id, "file_path": file_path, "caption": caption})

        async def send_reaction(self, chat_id: str, target_message_id: str,
                                target_sender_jid: str, emoji: str) -> str:
            return _record_delivery("whatsapp_react", {
                "chat_id": chat_id, "target_message_id": target_message_id,
                "emoji": emoji})

        async def _send_ack(self, message_id: str) -> None:
            return None

        async def subscribe_presence(self, chat_id: str) -> None:
            return None

        async def start_typing(self, chat_id: str) -> None:
            return None

        async def stop_typing(self, chat_id: str) -> None:
            return None

        @property
        def connected(self) -> bool:  # outreach tools gate on a live bridge
            return True

    bridge = FakeBridge(ctx)
    bridge._connected = True
    return bridge


def _record_delivery(kind: str, payload: dict[str, Any]) -> str:
    w = world()
    if w is None:
        raise RuntimeError(f"eval: {kind} outside a world")
    return w.record_delivery(kind, payload)


# --------------------------------------------------------------------------
# external effect kinds
# --------------------------------------------------------------------------

def _world_only(original: Any, fake: Any) -> Any:
    """Route to ``fake`` inside a world, to the original everywhere else —
    installing the boundary must never change behaviour outside worlds
    (the pytest session, other code sharing the process)."""
    if asyncio.iscoroutinefunction(original) or asyncio.iscoroutinefunction(fake):
        async def wrapper(*a: Any, **k: Any) -> Any:
            if world() is None:
                return await original(*a, **k)
            return await fake(*a, **k)
    else:
        def wrapper(*a: Any, **k: Any) -> Any:  # type: ignore[misc]
            if world() is None:
                return original(*a, **k)
            return fake(*a, **k)
    wrapper._bob_eval_original = original  # type: ignore[attr-defined]
    return wrapper


def _unwrapped(fn: Any) -> Any:
    return getattr(fn, "_bob_eval_original", fn)


def _install_effect_executors() -> None:
    # Import the modules that register real executors at import time FIRST,
    # so ours are registered last and win.
    import server.services.email_tools  # noqa: F401  # pyright: ignore[reportUnusedImport]
    import server.services.subagent_service  # noqa: F401  # pyright: ignore[reportUnusedImport]
    from server.services import effects as effects_svc

    def real(kind: str) -> Any:
        fn = effects_svc._EXECUTORS.get(kind, (None, True))[0]
        return _unwrapped(fn) if fn else None

    async def _missing(ctx, payload):
        raise RuntimeError("no executor")

    async def _email_reply(ctx, payload):
        return _record_delivery("email_reply", payload)

    async def _email_send(ctx, payload):
        return _record_delivery("email_send", payload)

    real_spawn = real("subagent_spawn")

    async def _spawn(ctx, payload):
        sid = await world().record_spawn(payload)
        if world().real_subagents and real_spawn is not None:
            return await real_spawn(ctx, payload)   # cwd = the world workspace
        return sid

    for kind, fake in (("email_reply", _email_reply), ("email_send", _email_send),
                       ("subagent_spawn", _spawn)):
        retryable = effects_svc._EXECUTORS.get(kind, (None, True))[1]
        effects_svc.register_executor(
            kind, _world_only(real(kind) or _missing, fake), retryable=retryable)


def ensure_executors() -> None:
    """Re-assert the external executors (a FakeBridge construction
    re-registers the WhatsApp ones — those route to FakeBridge transport,
    which is what we want; email/spawn must stay ours)."""
    _install_effect_executors()


# --------------------------------------------------------------------------
# process supervision (bg jobs)
# --------------------------------------------------------------------------

def _install_process_fakes() -> None:
    from server.services import process_tools as pt
    if getattr(pt._run_cmd, "_bob_eval_original", None):
        return

    async def _refuse(cmd: list[str]) -> tuple[int, str]:
        logger.error("eval: blocked host command %s", cmd)
        return 1, "eval: host process control is blocked inside a world"

    async def _spawn_systemd(ctx, name, command, log, **kwargs):
        return world().record_bg_start(name, command, log)

    async def _spawn_setsid(ctx, name, command, log):
        return world().record_bg_start(name, command, log)

    async def _terminate(entry: dict) -> str:
        world().record_bg_stop(entry.get("name", ""))
        return "stopped"

    async def _unit_state(unit: str) -> dict[str, str]:
        # Mirrors a RemainAfterExit wake unit: running, then active(exited).
        name = unit.removeprefix(pt._UNIT_PREFIX).removesuffix(".service")
        if world().bg_alive(name):
            return {"LoadState": "loaded", "ActiveState": "active",
                    "SubState": "running", "Result": "success",
                    "ExecMainStatus": "0"}
        code = world().bg_exit(name)
        return {"LoadState": "loaded", "ActiveState": "active",
                "SubState": "exited",
                "Result": "success" if code == 0 else "exit-code",
                "ExecMainStatus": "" if code is None else str(code)}

    async def _entry_alive(entry: dict) -> bool:
        return world().bg_alive(entry.get("name", ""))

    pt._run_cmd = _world_only(pt._run_cmd, _refuse)
    pt._spawn_systemd = _world_only(pt._spawn_systemd, _spawn_systemd)
    pt._spawn_setsid = _world_only(pt._spawn_setsid, _spawn_setsid)
    pt._terminate = _world_only(pt._terminate, _terminate)
    pt._unit_state = _world_only(pt._unit_state, _unit_state)
    pt._entry_alive = _world_only(pt._entry_alive, _entry_alive)
    pt._pid_start_time = _world_only(pt._pid_start_time, lambda pid: None)
    # The real check is a module constant read at spawn time; inside a world
    # the faked systemd path is always "available" (the setsid fake records
    # the same way, so the value only picks the label).


# --------------------------------------------------------------------------
# voice, Home Assistant, attention
# --------------------------------------------------------------------------

def _install_subagent_fake() -> None:
    """Claude Code launches (spawn executor runs, message_subagent resumes)
    record instead of running — unless the world opted into real ones."""
    from server.services.subagent_service import SubagentService
    real = SubagentService._run_claude
    if getattr(real, "_bob_eval_original", None):
        return

    async def _run_claude(self, *a, **kw):
        if world().real_subagents:
            return await real(self, *a, **kw)
        return world().record_claude_run(*a, **kw)

    SubagentService._run_claude = _world_only(real, _run_claude)  # type: ignore[method-assign]


class _Routed:
    """A dict/set stand-in that forwards to the current world's own
    container (``getattr(world, attr)``), or to the original outside one."""

    def __init__(self, original: Any, attr: str) -> None:
        self._original = original
        self._attr = attr

    def _target(self) -> Any:
        w = world()
        return getattr(w, self._attr) if w is not None else self._original

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)

    def __contains__(self, item: Any) -> bool:
        return item in self._target()

    def __iter__(self):
        return iter(self._target())

    def __len__(self) -> int:
        return len(self._target())

    def __getitem__(self, key: Any) -> Any:
        return self._target()[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        self._target()[key] = value

    def __delitem__(self, key: Any) -> None:
        del self._target()[key]


def _install_misc_fakes() -> None:
    from server.services.attention.coordinator import AttentionCoordinator
    from server.services.homeassistant_client import HomeAssistantClient
    from server.services.voice_dispatch_service import VoiceDispatchService
    if getattr(AttentionCoordinator.submit, "_bob_eval_original", None):
        return

    async def dispatch_contact_call(self, subagent_id, task, contact_id,
                                    modality, parent_session_key, *a, **k):
        return world().record_call(subagent_id=subagent_id, task=task,
                                   contact_id=contact_id, modality=modality)

    async def get_state(self, entity_id, *, force_refresh=False):
        return world().ha_states.get(entity_id)

    real_submit, real_resume = (
        AttentionCoordinator.submit, AttentionCoordinator.resume_pending)
    real_typing = AttentionCoordinator.notify_typing

    # The bypass dispatches at once but on its OWN task, as the real window
    # does: a wake raised inside a turn (closing a promise wakes its
    # waiter — often the same conversation) must not run inline while that
    # turn still holds the session lock (2026-10-08: deadlocked close_goal).
    # The task inherits the world context, so settle() waits for it.
    async def submit(self, session_key, dispatch_fn, **kwargs):
        if world().attention_real:
            return await real_submit(self, session_key, dispatch_fn, **kwargs)
        asyncio.ensure_future(dispatch_fn())

    async def resume_pending(self, session_key, dispatch_fn, *a, **kwargs):
        if world().attention_real:
            return await real_resume(self, session_key, dispatch_fn, *a, **kwargs)
        asyncio.ensure_future(dispatch_fn())

    def notify_typing(self, *a, **k):
        if world().attention_real:
            return real_typing(self, *a, **k)
        return None

    # Window state is class-level and keyed by session — concurrent worlds
    # share the cast's session keys, so route it per world.
    AttentionCoordinator._windows = _Routed(AttentionCoordinator._windows,
                                            "attention_windows")
    AttentionCoordinator._dispatching = _Routed(AttentionCoordinator._dispatching,
                                                "attention_dispatching")
    # Per-session dispatch locks are class-level too: without routing,
    # concurrent worlds serialise on the cast's shared session keys.
    from server.services.session_dispatch_gate import SessionDispatchGate
    SessionDispatchGate._locks = _Routed(SessionDispatchGate._locks, "dispatch_locks")
    # The due-notice coalescer is module-level and keyed by conversation:
    # across worlds it swallowed a later trial's reminder as a 'sibling'.
    from server.services import goal_service
    if isinstance(getattr(goal_service, "_OVERDUE_LAST_WAKE", None), dict):
        goal_service._OVERDUE_LAST_WAKE = _Routed(
            goal_service._OVERDUE_LAST_WAKE, "overdue_last_wake")

    V, H, A = VoiceDispatchService, HomeAssistantClient, AttentionCoordinator
    V.dispatch_contact_call = _world_only(V.dispatch_contact_call, dispatch_contact_call)  # type: ignore[method-assign]
    H.get_state = _world_only(H.get_state, get_state)  # type: ignore[method-assign]
    A.submit = _world_only(A.submit, submit)  # type: ignore[method-assign]
    A.resume_pending = _world_only(A.resume_pending, resume_pending)  # type: ignore[method-assign]
    A.notify_typing = _world_only(A.notify_typing, notify_typing)  # type: ignore[method-assign]


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------

def make_fixture_mcp(ctx: Any, servers: list[dict[str, Any]]) -> Any:
    """A real McpManager whose cache is the world's fixture servers and
    whose call_tool answers from their handlers. make_mcp_tools /
    mcp_transparency_note run unmodified against the fixture rows."""
    from server.services.mcp_service import McpManager, ServerToolCache

    class FixtureMcpManager(McpManager):
        async def start(self) -> None:
            return None

        async def call_tool(self, server_name, tool_name, args):
            w = world()
            handler = w.mcp_handlers.get((server_name, tool_name)) if w else None
            if handler is None:
                return f"Error: MCP tool {tool_name} ({server_name}) unavailable"
            out = handler(**args)
            if asyncio.iscoroutine(out):
                out = await out
            return out if isinstance(out, str) else json.dumps(out)

    mgr = FixtureMcpManager(ctx)
    for s in servers:
        mgr._cache[s["id"]] = ServerToolCache(
            server_id=s["id"], name=s["name"], fetched_at=time.monotonic(),
            tools=[{"name": t["name"], "description": t["description"],
                    "input_schema": t["input_schema"]} for t in s["tools"]])
    return mgr


# --------------------------------------------------------------------------
# network: the fixture API gateway + scrubbed skill env
# --------------------------------------------------------------------------

_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|SID|CREDENTIAL|TWILIO|"
                         r"COOKIE|WEBHOOK", re.I)
_GATEWAY: Any = None
_GATEWAY_WORLDS: dict[str, "World"] = {}


def _start_gateway() -> Any:
    """In-process HTTP server standing in for the paid APIs planted skill
    scripts call (OpenAI images, Runware video/3D). Scripts are ordinary
    API clients; the world points their base URL here, so they work end
    to end — inspectable, credible, and free. Requests are recorded on the
    calling world (path /w/<world-id>/<service>/...)."""
    import base64
    import threading
    import uuid as _uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from server.evals.cases._kit import canon_image

    tasks: dict[str, dict[str, Any]] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            return None

        def _reply(self, code: int, body: Any, ctype: str = "application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method: str) -> None:
            parts = self.path.split("?")[0].strip("/").split("/")
            if len(parts) < 3 or parts[0] != "w":
                return self._reply(404, {"error": "not found"})
            w = _GATEWAY_WORLDS.get(parts[1])
            service, rest = parts[2], "/".join(parts[3:])
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if w is not None and service in w.api_down:
                w.api_calls.append({"service": service, "method": method,
                                    "path": rest, "refused": True})
                return self._reply(503, {"error": w.api_down[service]})
            if w is not None:
                w.api_calls.append({"service": service, "method": method,
                                    "path": rest, "bytes": len(raw),
                                    "body": raw[:2000].decode("utf-8", "replace")})
            if service == "openai" and rest.startswith("v1/images/") \
                    and w is not None and w.real_image_api:
                return self._proxy_openai(w, rest, raw)
            if service == "openai" and rest.startswith("v1/images/"):
                png = canon_image("refpack-08-cartoon.png")
                return self._reply(200, {"created": int(time.time()), "data": [
                    {"b64_json": base64.b64encode(png).decode()}]})
            if service == "runware" and method == "POST":
                tid = str(_uuid.uuid4())
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = {}
                tasks[tid] = {"t0": time.monotonic(), "kind": rest,
                              "payload": payload, "world": w}
                return self._reply(200, {"taskUUID": tid, "status": "processing"})
            if service == "runware" and rest.startswith("v1/tasks/"):
                tid = rest.split("/")[-1]
                t = tasks.get(tid)
                if t is None:
                    return self._reply(404, {"error": "unknown task"})
                if time.monotonic() - t["t0"] < 20:
                    return self._reply(200, {"taskUUID": tid, "status": "processing"})
                base = f"http://127.0.0.1:{self.server.server_port}/w/{parts[1]}/runware"
                return self._reply(200, {"taskUUID": tid, "status": "completed",
                                         "artifactURL": f"{base}/v1/artifacts/{tid}"})
            if service == "runware" and rest.startswith("v1/artifacts/"):
                t = tasks.get(rest.split("/")[-1]) or {}
                return self._reply(200, _render_artifact(t), "video/mp4")
            return self._reply(404, {"error": f"no fixture for {service}/{rest}"})

        def _proxy_openai(self, w: Any, rest: str, raw: bytes) -> None:
            """Opt-in (w.real_image_api): forward to the real OpenAI images
            API with the eval process's key, quality forced to low. For
            cases where what the image SHOWS is under test — a canned image
            gets inspected and correctly called broken."""
            import os
            import urllib.error
            import urllib.request
            ctype = self.headers.get("Content-Type", "application/json")
            body = raw
            if ctype.startswith("application/json"):
                data = json.loads(raw or b"{}")
                data["quality"] = "low"
                body = json.dumps(data).encode()
            elif "boundary=" in ctype:
                b = ctype.split("boundary=", 1)[1].strip().encode()
                field = (b"--" + b + b'\r\nContent-Disposition: form-data; '
                         b'name="quality"\r\n\r\nlow\r\n')
                body = raw.replace(b"--" + b + b"--", field + b"--" + b + b"--", 1)
            key = os.getenv("BOB_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
            req = urllib.request.Request(
                f"https://api.openai.com/{rest}", data=body, method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": ctype})
            w.api_calls[-1]["real"] = True
            try:
                with urllib.request.urlopen(req, timeout=240) as r:
                    return self._reply(r.status, r.read())
            except urllib.error.HTTPError as e:
                return self._reply(e.code, e.read())

        def do_POST(self) -> None:
            self._route("POST")

        def do_GET(self) -> None:
            self._route("GET")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, name="eval-gateway",
                     daemon=True).start()
    return server


def _render_artifact(task: dict) -> bytes:
    """What a video API would hand back for THIS request: a slow zoom over
    the actual input image at the requested size and duration. A fixed
    test pattern got inspected (ffprobe: 64x64, 2s for a 5s 1024x1536 ask)
    and correctly called a fake, 2026-10-08."""
    import subprocess
    import tempfile
    from pathlib import Path

    from server.evals.cases._kit import canon_image, real_mp4
    payload = task.get("payload") or {}
    w = task.get("world")
    seconds = max(1, min(int(payload.get("duration") or 5), 10))
    try:
        width, height = (int(x) for x in str(payload.get("size") or "1280x720").split("x"))
    except ValueError:
        width, height = 1280, 720
    width, height = width - width % 2, height - height % 2
    src = None
    for ref in payload.get("inputImages") or [payload.get("inputImage")]:
        if ref and w is not None:
            cand = (w.workspace / str(ref)) if not str(ref).startswith("/") else Path(ref)
            if cand.is_file():
                src = cand
                break
    with tempfile.TemporaryDirectory() as tmp:
        if src is None:
            src = Path(tmp) / "in.png"
            src.write_bytes(canon_image("refpack-12-pint.png"))
        out = f"{tmp}/out.mp4"
        frames = seconds * 24
        vf = (f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
              f"crop={width * 2}:{height * 2},"
              f"zoompan=z='min(zoom+0.0008,1.15)':d={frames}:s={width}x{height}:fps=24")
        try:
            subprocess.run(["ffmpeg", "-loglevel", "error", "-loop", "1", "-i", str(src),
                            "-vf", vf, "-t", str(seconds), "-pix_fmt", "yuv420p",
                            "-c:v", "libx264", "-preset", "ultrafast",
                            "-movflags", "+faststart", out],
                           check=True, timeout=120)
            return open(out, "rb").read()
        except Exception:
            return real_mp4(seconds)


def gateway_url(w: "World", service: str) -> str:
    _GATEWAY_WORLDS[w.id] = w
    return f"http://127.0.0.1:{_GATEWAY.server_port}/w/{w.id}/{service}"


def release_world(w: "World") -> None:
    _GATEWAY_WORLDS.pop(w.id, None)


def _install_skill_env() -> None:
    """World bash/bg env: production's env minus every secret, plus the
    gateway endpoints. A world can't spend real money or reach real APIs
    with Bob's keys."""
    from server.services import process_tools, workspace_tools
    from server.services.skill_env import build_skill_env as original

    if getattr(workspace_tools.build_skill_env, "_bob_eval", False):
        return

    def build_skill_env(base_env=None, *, workspace_dir=None, venv_dir=None):
        env = original(base_env, workspace_dir=workspace_dir, venv_dir=venv_dir)
        w = world()
        if w is None:
            return env
        for k in list(env):
            if _SECRET_ENV.search(k):
                env.pop(k)
        env["OPENAI_API_KEY"] = "sk-eval-world-key"
        env["OPENAI_BASE_URL"] = gateway_url(w, "openai") + "/v1"
        env["RUNWARE_API_BASE"] = gateway_url(w, "runware") + "/v1"
        env["HOME"] = str(w.root / "home")
        return env

    build_skill_env._bob_eval = True  # type: ignore[attr-defined]
    workspace_tools.build_skill_env = build_skill_env
    process_tools.build_skill_env = build_skill_env


class FakeAgentMail:
    """AgentMail transport stand-in for the email channel's ingress side
    (mark-read, refetch). Outbound mail rides the email_* effect kinds."""

    async def update_message(self, *a: Any, **k: Any) -> dict:
        return {}

    async def list_messages(self, *a: Any, **k: Any) -> list:
        return []

    async def get_message(self, *a: Any, **k: Any) -> dict:
        raise RuntimeError("eval: no remote mailbox")

    async def get_attachment(self, *a: Any, **k: Any) -> bytes:
        raise RuntimeError("eval: no remote attachments")


# --------------------------------------------------------------------------
# task tracking
# --------------------------------------------------------------------------

def _install_embedding_gate() -> None:
    """Honour eval_fixtures.SKIP_EMBEDDING: seeding writes embed per entity
    in production code; worlds embed once in a batch instead."""
    from server.services.memory import embedding
    from server.services.memory.eval_fixtures import SKIP_EMBEDDING
    if getattr(embedding.embed_batch, "_bob_eval", False):
        return
    real = embedding.embed_batch

    async def embed_batch(texts):
        if SKIP_EMBEDDING.get():
            return [None] * len(texts)
        return await real(texts)

    embed_batch._bob_eval = True  # type: ignore[attr-defined]
    embedding.embed_batch = embed_batch


def _install_task_tracker() -> None:
    """Remember every task created inside a world so World.settle() can
    wait for its background work (wake dispatches, backburner flights)."""
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()

    def factory(loop, coro, **kwargs):
        if previous is not None:
            task = previous(loop, coro, **kwargs)
        else:
            task = asyncio.Task(coro, loop=loop, **kwargs)
        w = kwargs.get("context", contextvars.copy_context()).get(CURRENT_WORLD)
        if w is not None:
            w.track_task(task)
        return task

    loop.set_task_factory(factory)


def install() -> None:
    """Idempotent, process-wide. Call from inside the running loop."""
    global _installed
    if _installed:
        _install_task_tracker()
        return
    _install_probe()
    _install_hosted_tool_probe()
    _install_effect_executors()
    _install_process_fakes()
    _install_misc_fakes()
    _install_subagent_fake()
    _install_embedding_gate()
    _install_skill_env()
    global _GATEWAY
    _GATEWAY = _start_gateway()
    _install_task_tracker()
    _installed = True
