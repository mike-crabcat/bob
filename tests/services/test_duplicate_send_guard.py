"""Duplicate-send guard (2026-09-13): the GLM re-emission quirk repeats
a send seconds after it succeeded — same dispatch, seq 0 and 1, two pairs
in one morning (08:47:57/58 and 08:59:23/26, both delivered because the
seq-keyed effects outbox is per-call by design). The send closure now
refuses an exact re-send of text already delivered that turn."""

from __future__ import annotations

from server.services.whatsapp_bridge_service._service import (
    is_duplicate_send,
)


def test_exact_repeat_is_duplicate():
    sent = ["It aired — this morning 07:22."]
    assert is_duplicate_send("It aired — this morning 07:22.", sent)


def test_different_text_passes():
    sent = ["It aired — this morning 07:22."]
    assert not is_duplicate_send("Different message.", sent)


def test_captioned_media_form_counts():
    # media sends record "[Image: caption]" in sent_texts — a plain
    # re-send of the same caption must still be caught
    sent = ["[Image: Advert copy here]"]
    assert is_duplicate_send("Advert copy here", sent)


def test_empty_text_never_blocks():
    # media-only sends carry no caption — must not trip the guard
    assert not is_duplicate_send("", ["", "[Image: ]"])
