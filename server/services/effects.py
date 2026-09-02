"""Effects — the outbox boundary (Bob3 Phase IV).

Every external action taken from an LLM turn is recorded durably in the
``effects`` table BEFORE delivery (invariant 7), then delivered through a
registered executor. Delivery is write-ahead-inline: the emitting code path
records the effect and immediately runs the executor, so user-facing latency
is unchanged — but a crash between record and delivery leaves a pending row
that the background pump retries with backoff. Idempotency keys make the
record step at-most-once, and delivery claims are compare-and-set on status,
so retrying can never duplicate a delivered effect.

Executors are registered per kind at service start:
- ``whatsapp_send`` / ``whatsapp_send_media`` — WhatsApp bridge (retryable)
- ``email_reply`` / ``email_send`` — AgentMail delivery (retryable)
- non-retryable kinds (e.g. call placement) fail straight to dead: a
  duplicate phone call is worse than a lost one.

Observed results are appended to the event log (``effect.delivered`` /
``effect.failed``) — best-effort, never blocking delivery itself.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# kind -> (executor(ctx, payload) -> external_result_id | None, pump_retryable)
_EXECUTORS: dict[str, tuple[Callable[[Any, dict], Awaitable[str | None]], bool]] = {}


def register_executor(
    kind: str,
    fn: Callable[[Any, dict], Awaitable[str | None]],
    *,
    retryable: bool = True,
) -> None:
    _EXECUTORS[kind] = (fn, retryable)


def registered_kinds(*, retryable_only: bool = False) -> list[str]:
    return [k for k, (_, r) in _EXECUTORS.items() if r or not retryable_only]


def executors_reset_for_tests() -> None:
    _EXECUTORS.clear()


class FakeEffectSink:
    """Replay-safe effect sink (Bob3 invariant 8): while installed, EVERY
    effect kind is captured here instead of reaching a real executor, so a
    recorded episode can replay through real Act code with zero external
    actions. Enforced by type — delivery checks the sink before the registry.
    """

    def __init__(self) -> None:
        self.delivered: list[dict[str, Any]] = []

    async def __call__(self, ctx: Any, kind: str, payload: dict[str, Any]) -> str:
        self.delivered.append({"kind": kind, "payload": payload})
        return f"fake-{len(self.delivered)}"


_FAKE_SINK: FakeEffectSink | None = None


def install_fake_sink(sink: FakeEffectSink) -> None:
    if not isinstance(sink, FakeEffectSink):
        raise TypeError("replay sink must be a FakeEffectSink")
    global _FAKE_SINK
    _FAKE_SINK = sink


def uninstall_fake_sink() -> None:
    global _FAKE_SINK
    _FAKE_SINK = None


async def emit_and_deliver(
    ctx: Any,
    *,
    kind: str,
    idempotency_key: str,
    payload: dict[str, Any],
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Record an effect and deliver it inline. Returns a result dict:

    ``{"ok": bool, "effect_id", "external_result_id"?, "error"?, "duplicate"?}``
    """
    from bob_server.repositories.effects import EffectRepository

    repo = EffectRepository(ctx.db)
    effect_id = await repo.emit(
        kind=kind, idempotency_key=idempotency_key, payload=payload,
        turn_id=turn_id)
    if effect_id is None:
        existing = await repo.get_by_key(idempotency_key)
        logger.info("effect duplicate suppressed: key=%s status=%s",
                    idempotency_key, existing["status"] if existing else "?")
        return {"ok": True, "duplicate": True,
                "effect_id": existing["id"] if existing else None}

    claimed = await _claim_one(ctx, effect_id)
    if claimed is None:
        # Another owner (the pump) got it first — treat as accepted.
        return {"ok": True, "effect_id": effect_id, "queued": True}
    return await deliver(ctx, claimed)


async def deliver(ctx: Any, effect: dict[str, Any]) -> dict[str, Any]:
    """Run the executor for a claimed ('delivering') effect and record the outcome."""
    import json as _json

    from bob_server.repositories.effects import EffectRepository

    repo = EffectRepository(ctx.db)
    kind = effect["kind"]
    if _FAKE_SINK is not None:
        payload = effect["payload_json"]
        if isinstance(payload, str):
            payload = _json.loads(payload or "{}")
        external_id = await _FAKE_SINK(ctx, kind, payload)
        await repo.mark_delivered(effect["id"], external_result_id=external_id)
        return {"ok": True, "effect_id": effect["id"],
                "external_result_id": external_id, "fake": True}
    entry = _EXECUTORS.get(kind)
    if entry is None:
        await repo.mark_failed(effect["id"], f"no executor registered for kind {kind!r}")
        return {"ok": False, "effect_id": effect["id"],
                "error": f"no executor for {kind}"}
    fn, retryable = entry
    payload = effect["payload_json"]
    if isinstance(payload, str):
        payload = _json.loads(payload or "{}")

    try:
        external_id = await fn(ctx, payload)
    except Exception as exc:
        if retryable:
            status = await repo.mark_failed(effect["id"], str(exc))
        else:
            await repo.mark_failed(effect["id"], str(exc))
            # Non-retryable: force dead regardless of attempt count.
            await ctx.db.execute(
                "UPDATE effects SET status = 'dead' WHERE id = ? AND status != 'delivered'",
                (effect["id"],))
            status = "dead"
        logger.warning("effect %s (%s) delivery failed -> %s: %s",
                       effect["id"], kind, status, exc)
        await _append_result_event(ctx, effect, "effect.failed", error=str(exc))
        return {"ok": False, "effect_id": effect["id"], "error": str(exc),
                "status": status}

    await repo.mark_delivered(effect["id"], external_result_id=external_id)
    await _append_result_event(ctx, effect, "effect.delivered",
                               external_result_id=external_id)
    return {"ok": True, "effect_id": effect["id"],
            "external_result_id": external_id}


async def pump_due_effects(ctx: Any, *, limit: int = 20) -> int:
    """Claim and deliver due pending effects (crash leftovers + backoff retries).

    Also requeues effects stuck in 'delivering' past the staleness window —
    a crash between claim and outcome write would otherwise lose them (the
    pump only ever claims 'pending'). Returns the number of effects
    processed. Called by the heartbeat task.
    """
    from bob_server.repositories.effects import EffectRepository

    kinds = registered_kinds(retryable_only=True)
    if not kinds:
        return 0
    repo = EffectRepository(ctx.db)
    requeued = await repo.requeue_stale_delivering()
    if requeued:
        logger.info("effect pump requeued %d stuck delivering effect(s)", requeued)
    claimed = await repo.claim_due(kinds=kinds, limit=limit)
    for effect in claimed:
        await deliver(ctx, effect)
    return len(claimed)


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def _claim_one(ctx: Any, effect_id: str) -> dict[str, Any] | None:
    async with ctx.db.transaction() as txn:
        row = await txn.fetch_one(
            "SELECT * FROM effects WHERE id = ? AND status = 'pending'",
            (effect_id,))
        if row is None:
            return None
        await txn.execute(
            """UPDATE effects SET status = 'delivering', attempt = attempt + 1,
               claimed_at = ? WHERE id = ?""",
            (_iso_now(), effect_id))
        row["attempt"] = row["attempt"] + 1
        return row


async def _append_result_event(ctx: Any, effect: dict[str, Any],
                               event_type: str, **extra: Any) -> None:
    try:
        from bob_server.repositories import Event, EventLogRepository

        await EventLogRepository(ctx.db).append(Event(
            event_type=event_type,
            binding_key=f"effect:{effect['kind']}",
            conversation_id=effect.get("turn_id") or f"effect:{effect['id']}",
            source="effects",
            external_id=f"{effect['id']}:{event_type}",
            causation_id=None,
            payload={"effect_id": effect["id"], "kind": effect["kind"],
                     "attempt": effect.get("attempt"), **extra},
        ))
    except Exception:
        logger.warning("effect result event append failed", exc_info=True)
