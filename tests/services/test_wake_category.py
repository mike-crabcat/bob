"""Wake-path call-category labelling (2026-09-24).

Stimulus-steered crypto signal wakes were logged as whatsapp_incoming —
indistinguishable from human turns (~90% of the Crypto-Bob group's
"incoming" volume was machinery). _resolve_wake_category splits them:
raw human rows keep whatsapp_incoming, pure wake batches log their trigger.
"""

from __future__ import annotations

from server.services.whatsapp_bridge_service._service import (
    _resolve_wake_category,
)

SESSION = "agent:main:whatsapp:group:120363410716086644"


async def _seed(db, *provenances: str | None) -> None:
    for i, prov in enumerate(provenances):
        await db.execute(
            "INSERT INTO messages (id, conversation_id, role, content, "
            "dispatched, provenance, created_at) VALUES (?, ?, 'user', ?, 0, ?, "
            "datetime('now', ?))",
            (f"msg-{i}", SESSION, f"row {i}", prov, f"-{i} minutes"))


async def test_human_rows_stay_whatsapp_incoming_even_with_request(db):
    await _seed(db, "steer", None)  # signal wake + raw human text
    assert await _resolve_wake_category(db, SESSION, "steer") == "whatsapp_incoming"


async def test_requested_category_on_pure_wake_batch(db):
    await _seed(db, "steer", "steer")
    assert await _resolve_wake_category(db, SESSION, "steer") == "steer"


async def test_provenance_derived_when_no_request(db):
    # crash-recovery sweep re-arms rows with no category to pass
    await _seed(db, "routine")
    assert await _resolve_wake_category(db, SESSION, None) == "routine"


async def test_mixed_provenances_fall_back_to_wakeup(db):
    await _seed(db, "steer", "wake_nudge")
    assert await _resolve_wake_category(db, SESSION, None) == "wakeup"


async def test_empty_batch_is_wakeup(db):
    assert await _resolve_wake_category(db, SESSION, None) == "wakeup"
