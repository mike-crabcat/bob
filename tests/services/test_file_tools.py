"""Structured file tools (read_file / edit_file / multi_edit) and the
hardened workspace path resolver (2026-10-12)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.services.file_tools import make_file_tools
from server.services.workspace_tools import _resolve_path, _resolve_writable


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "workspace"
    w.mkdir()
    return w


@pytest.fixture
def ctx(ws):
    return SimpleNamespace(settings=SimpleNamespace(
        harness=SimpleNamespace(workspace_dir=ws)))


@pytest.fixture
def tools(ctx):
    return {t.name: t.handler for t in make_file_tools(ctx)}


# ------------------------------------------------------------- resolver

def test_relative_symlink_escape_is_refused(ctx, ws, tmp_path):
    outside = tmp_path / "secrets"
    outside.mkdir()
    (outside / "key").write_text("s3cret")
    (ws / "link").symlink_to(outside)
    with pytest.raises(ValueError, match="outside the workspace"):
        _resolve_path(ctx, "link/key")


def test_absolute_symlink_escape_is_refused(ctx, ws, tmp_path):
    outside = tmp_path / "data"
    outside.mkdir()
    (ws / "d").symlink_to(outside)
    with pytest.raises(ValueError, match="outside the workspace"):
        _resolve_path(ctx, str(ws / "d" / "bob.db"))


def test_dotdot_and_outside_absolute_refused(ctx, tmp_path):
    with pytest.raises(ValueError):
        _resolve_path(ctx, "../x")
    with pytest.raises(ValueError):
        _resolve_path(ctx, str(tmp_path / "elsewhere.txt"))


def test_in_workspace_paths_resolve(ctx, ws):
    (ws / "a.txt").write_text("x")
    assert _resolve_path(ctx, "a.txt") == (ws / "a.txt").resolve()
    assert _resolve_path(ctx, str(ws / "a.txt")) == (ws / "a.txt").resolve()


def test_persona_view_is_not_writable(ctx, ws):
    (ws / "self/bob").mkdir(parents=True)
    (ws / "self/bob/soul.md").write_text("soul")
    assert _resolve_path(ctx, "self/bob/soul.md")  # readable
    with pytest.raises(ValueError, match="read-only persona view"):
        _resolve_writable(ctx, "self/bob/soul.md")


# ------------------------------------------------------------- read_file

async def test_read_file_numbers_and_pages(tools, ws):
    (ws / "big.txt").write_text("\n".join(f"line {i}" for i in range(1, 1001)))
    out = await tools["read_file"]("big.txt", offset=1, limit=10)
    assert "1000 lines; showing 1-10" in out and "offset=11" in out
    assert "     1\tline 1" in out and "    10\tline 10" in out
    assert "line 11" not in out
    tail = await tools["read_file"]("big.txt", offset=995, limit=50)
    assert "showing 995-1000" in tail and "offset=" not in tail


async def test_read_file_caps_long_lines_and_total(tools, ws):
    (ws / "wide.txt").write_text(("x" * 5000 + "\n") * 50)
    out = await tools["read_file"]("wide.txt", limit=50)
    assert "[line truncated]" in out
    assert len(out) < 35_000


async def test_read_file_redirects_media_and_binary(tools, ws):
    (ws / "pic.png").write_bytes(b"\x89PNG....")
    assert "read_image" in await tools["read_file"]("pic.png")
    (ws / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    assert "binary" in await tools["read_file"]("blob.bin")


async def test_read_file_offset_past_end_and_empty(tools, ws):
    (ws / "e.txt").write_text("")
    assert "empty file" in await tools["read_file"]("e.txt")
    (ws / "s.txt").write_text("one\ntwo\n")
    assert "past the end" in await tools["read_file"]("s.txt", offset=50)


# ------------------------------------------------------------- edit_file

async def test_edit_file_replaces_and_returns_diff(tools, ws):
    f = ws / "skill.py"
    f.write_text("def a():\n    return 1\n\ndef b():\n    return 2\n")
    out = await tools["edit_file"]("skill.py", "    return 2", "    return 3")
    assert out.startswith("Edited skill.py")
    assert "-    return 2" in out and "+    return 3" in out
    assert f.read_text().endswith("return 3\n")


async def test_edit_file_not_found_hints_closest(tools, ws):
    (ws / "c.py").write_text("alpha = 1\nbeta_value = 2\ngamma = 3\n")
    out = await tools["edit_file"]("c.py", "beta_valu = 2", "beta_value = 9")
    assert out.startswith("Error:") and "not found" in out
    assert "Closest lines" in out and "2\tbeta_value = 2" in out
    assert (ws / "c.py").read_text() == "alpha = 1\nbeta_value = 2\ngamma = 3\n"


async def test_edit_file_ambiguous_lists_lines_then_replace_all(tools, ws):
    f = ws / "dup.txt"
    f.write_text("x = 1\ny\nx = 1\nz\nx = 1\n")
    out = await tools["edit_file"]("dup.txt", "x = 1", "x = 2")
    assert "matches 3 places (lines 1, 3, 5)" in out
    assert f.read_text().count("x = 1") == 3
    ok = await tools["edit_file"]("dup.txt", "x = 1", "x = 2", replace_all=True)
    assert ok.startswith("Edited") and f.read_text().count("x = 2") == 3


async def test_edit_file_refusals(tools, ws):
    (ws / "r.txt").write_text("same")
    assert "identical" in await tools["edit_file"]("r.txt", "same", "same")
    assert "empty" in await tools["edit_file"]("r.txt", "", "x")
    assert "not an existing file" in await tools["edit_file"]("nope.txt", "a", "b")


async def test_edit_preserves_mode_and_crlf(tools, ws):
    f = ws / "run.sh"
    f.write_bytes(b"#!/bin/sh\r\necho hi\r\n")
    os.chmod(f, 0o755)
    await tools["edit_file"]("run.sh", "echo hi", "echo bye")
    assert f.read_bytes() == b"#!/bin/sh\r\necho bye\r\n"
    assert f.stat().st_mode & 0o777 == 0o755


async def test_edit_refuses_persona_view(tools, ws):
    (ws / "self/bob").mkdir(parents=True)
    (ws / "self/bob/agents.md").write_text("rules")
    out = await tools["edit_file"]("self/bob/agents.md", "rules", "none")
    assert "read-only persona view" in out
    assert (ws / "self/bob/agents.md").read_text() == "rules"


# ------------------------------------------------------------- multi_edit

async def test_multi_edit_applies_in_order(tools, ws):
    f = ws / "m.py"
    f.write_text("a = 1\nb = 2\nc = 3\n")
    out = await tools["multi_edit"]("m.py", [
        {"old_string": "a = 1", "new_string": "a = 10"},
        {"old_string": "a = 10\nb = 2", "new_string": "a = 10\nb = 20"},
    ])
    assert "2 edits" in out
    assert f.read_text() == "a = 10\nb = 20\nc = 3\n"


async def test_multi_edit_is_all_or_nothing(tools, ws):
    f = ws / "m.py"
    f.write_text("a = 1\nb = 2\n")
    out = await tools["multi_edit"]("m.py", [
        {"old_string": "a = 1", "new_string": "a = 10"},
        {"old_string": "missing", "new_string": "x"},
    ])
    assert "edit 2:" in out and "nothing written" in out
    assert f.read_text() == "a = 1\nb = 2\n"


def test_multi_edit_schema_spells_out_the_edit_shape(ctx):
    t = {t.name: t for t in make_file_tools(ctx)}["multi_edit"]
    items = t.parameters["edits"]["items"]
    assert items["type"] == "object"
    assert set(items["required"]) == {"old_string", "new_string"}


def test_kill_switch_drops_the_tools(monkeypatch, ctx):
    from server.services.workspace_tools import make_workspace_tools
    names = {t.name for t in make_workspace_tools(ctx)}
    assert {"read_file", "edit_file", "multi_edit"} <= names
    monkeypatch.setenv("BOB_FILE_TOOLS", "off")
    names = {t.name for t in make_workspace_tools(ctx)}
    assert not {"read_file", "edit_file", "multi_edit"} & names
