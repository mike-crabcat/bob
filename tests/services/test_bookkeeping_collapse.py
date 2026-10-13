"""Collapsing bookkeeping runs never folds what Bob actually said
(2026-10-13 guard channel duplicate pictures)."""

from __future__ import annotations

from server.services.prompt_assembler import _collapse_run


def _row(i, role, prov, content="x"):
    return {"id": f"row{i:08d}", "role": role, "provenance": prov,
            "content": content, "created_at": f"2026-10-13 13:3{i}:00"}


def test_delivered_bg_send_survives_the_collapse():
    run = [_row(1, "user", "bg_placeholder", "[bg turn detached]"),
           _row(2, "assistant", "bg_send", "[Image: Latest: garage, 21:30]"),
           _row(3, "user", "report", "[Report from agent:frigate-watch:utility] 21:30")]
    out = _collapse_run(run)
    assert any(r["provenance"] == "bg_send" for r in out)
    assert out[-1]["provenance"] == "report"
    # only one notice was foldable, so nothing is collapsed at all
    assert [r["id"] for r in out] == [r["id"] for r in run]


def test_notices_still_collapse_around_kept_speech():
    run = [_row(1, "user", "bg_placeholder"), _row(2, "user", "wake_nudge"),
           _row(3, "assistant", "bg_send", "[Image: pic]"),
           _row(4, "user", "steer"), _row(5, "user", "report")]
    out = _collapse_run(run)
    marker = out[0]
    assert marker["provenance"] == "collapsed_bookkeeping"
    assert "3 earlier bookkeeping event(s)" in marker["content"]
    assert [r["provenance"] for r in out[1:]] == ["bg_send", "report"]
    assert marker["sender_id"] is None          # shape parity (2026-10-01)
