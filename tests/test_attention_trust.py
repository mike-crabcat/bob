"""Attention coordinator dispatch-spec selection (trust batching).

A window batches every sender's messages into ONE turn, but each message's
dispatch spec was built with its own sender's trust level (tool gating is
per-sender at spec-build time). The window must fly the highest-trust spec
it has seen, not whichever message happened to arrive last — the incident:
Mike's trusted skill-dev directive executed under the untrusted spec of a
message that landed 4 seconds after it.
"""

from __future__ import annotations

import asyncio

import pytest

import server.services.attention.coordinator as coord_mod
from server.services.attention import AttentionCoordinator

SESSION = "agent:main:whatsapp:group:120363000000000001"


@pytest.fixture(autouse=True)
def _tiny_windows(monkeypatch):
    """Shrunken windows + clean coordinator state (replay-test idiom)."""
    monkeypatch.setattr(coord_mod, "WINDOW_ADDRESSED_S", 0.03)
    monkeypatch.setattr(coord_mod, "WINDOW_GROUP_S", 0.08)
    monkeypatch.setattr(coord_mod, "MAX_WAIT_S", 1.0)
    AttentionCoordinator.reset_all()
    yield
    AttentionCoordinator.reset_all()


def _recording_fn(ran: list[str], tag: str):
    async def fn() -> str:
        ran.append(tag)
        return tag
    return fn


async def test_trusted_spec_survives_later_untrusted_message(ctx):
    """The incident case: trusted directive, then an untrusted message 4s later."""
    ran: list[str] = []
    coord = AttentionCoordinator(ctx)
    await coord.submit(SESSION, _recording_fn(ran, "trusted"),
                       text="@Bob update the merch skill", chat_kind="group",
                       sender_name="Mike", is_trusted=True)
    await coord.submit(SESSION, _recording_fn(ran, "untrusted"),
                       text="how do I pay?", chat_kind="group",
                       sender_name="David", is_trusted=False)
    await asyncio.sleep(0.25)
    assert ran == ["trusted"]


async def test_untrusted_then_trusted_uses_trusted_spec(ctx):
    """Arrival order must not matter: a batch containing a trusted sender's
    message runs under the trusted spec (who is trusted stays visible
    per-message, so the model still sees who asked for what)."""
    ran: list[str] = []
    coord = AttentionCoordinator(ctx)
    await coord.submit(SESSION, _recording_fn(ran, "untrusted"),
                       text="how do I pay?", chat_kind="group",
                       sender_name="David", is_trusted=False)
    await coord.submit(SESSION, _recording_fn(ran, "trusted"),
                       text="@Bob update the merch skill", chat_kind="group",
                       sender_name="Mike", is_trusted=True)
    await asyncio.sleep(0.25)
    assert ran == ["trusted"]


async def test_untrusted_only_batch_flies_latest_spec(ctx):
    """No trusted sender: historical last-wins behaviour is preserved."""
    ran: list[str] = []
    coord = AttentionCoordinator(ctx)
    await coord.submit(SESSION, _recording_fn(ran, "first"),
                       text="did anyone see the game?", chat_kind="group",
                       sender_name="David", is_trusted=False)
    await coord.submit(SESSION, _recording_fn(ran, "second"),
                       text="anyone?", chat_kind="group",
                       sender_name="Sylvain", is_trusted=False)
    await asyncio.sleep(0.25)
    assert ran == ["second"]


async def test_trusted_window_tracks_any_trusted_across_rearm(ctx):
    """Once a trusted spec is chosen, later untrusted messages re-arm the
    timer (window still slides) but never displace it."""
    ran: list[str] = []
    coord = AttentionCoordinator(ctx)
    await coord.submit(SESSION, _recording_fn(ran, "trusted"),
                       text="@Bob update the merch skill", chat_kind="group",
                       sender_name="Mike", is_trusted=True)
    for i in range(3):
        await coord.submit(SESSION, _recording_fn(ran, f"untrusted-{i}"),
                           text="still here", chat_kind="group",
                           sender_name="David", is_trusted=False)
    await asyncio.sleep(0.25)
    assert ran == ["trusted"]
