"""Boot-time self-bundle heal + file-based persona render (2026-09-03).

The repo bundle (self/<name>/ + user.md) is the single source of truth for
the persona and avatar pack; the workspace is a healed copy. Pinned here:
- changed files are restored, extras pruned, unchanged files left untouched
- read-only bits are (re)applied after the heal — advisory discouragement
- user.md seeds only-if-missing and is never healed over
- the persona renders from the workspace files, with {host} substituted by
  plain replacement (a stray brace can never crash rendering)
"""

from __future__ import annotations

import os
import platform
import stat
from pathlib import Path
from types import SimpleNamespace

from server.services import self_bundle
from server.services.persona import render_persona


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        harness=SimpleNamespace(workspace_dir=tmp_path / "workspace"))


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    bundle = repo / "self" / "bob"
    (bundle / "avatar" / "reactions").mkdir(parents=True)
    (bundle / "soul.md").write_text("soul v1", encoding="utf-8")
    (bundle / "identity.md").write_text("identity v1 {host}", encoding="utf-8")
    (bundle / "avatar" / "reactions" / "clip.mp4").write_bytes(b"\x00mp4")
    (repo / "user.md").write_text("owner profile", encoding="utf-8")
    return repo


def test_first_heal_seeds_the_workspace(tmp_path):
    repo = _make_repo(tmp_path)
    ws = tmp_path / "workspace"

    summary = self_bundle.refresh_bundled_assets(_settings(tmp_path), repo_root=repo)

    assert summary["personas"]["bob"] == {"copied": 3, "removed": 0}
    assert summary["seeded_user_md"] is True
    assert (ws / "self/bob/soul.md").read_text(encoding="utf-8") == "soul v1"
    assert (ws / "self/bob/avatar/reactions/clip.mp4").exists()
    assert (ws / "user.md").read_text(encoding="utf-8") == "owner profile"


def test_second_heal_is_a_content_noop(tmp_path):
    repo = _make_repo(tmp_path)
    ws = tmp_path / "workspace"
    self_bundle.refresh_bundled_assets(_settings(tmp_path), repo_root=repo)

    identity = ws / "self/bob/identity.md"
    mtime = identity.stat().st_mtime_ns
    summary = self_bundle.refresh_bundled_assets(_settings(tmp_path), repo_root=repo)

    assert summary["personas"]["bob"] == {"copied": 0, "removed": 0}
    # unchanged files are left byte-untouched — mtime stable, so the
    # prompt cache keyed on persona mtimes survives a no-op heal
    assert identity.stat().st_mtime_ns == mtime


def test_heal_restores_tampered_files_and_prunes_extras(tmp_path):
    repo = _make_repo(tmp_path)
    ws = tmp_path / "workspace"
    self_bundle.refresh_bundled_assets(_settings(tmp_path), repo_root=repo)

    # Corrupt a canon file (as the agent could — same uid), add a rogue
    # clip the bundle doesn't have.
    soul = ws / "self/bob/soul.md"
    os.chmod(soul, 0o644)
    soul.write_text("tampered", encoding="utf-8")
    reactions = ws / "self/bob/avatar/reactions"
    os.chmod(reactions, 0o755)
    (reactions / "rogue.mp4").write_bytes(b"\x00rogue")

    summary = self_bundle.refresh_bundled_assets(_settings(tmp_path), repo_root=repo)

    assert summary["personas"]["bob"] == {"copied": 1, "removed": 1}
    assert soul.read_text(encoding="utf-8") == "soul v1"  # healed
    assert not (reactions / "rogue.mp4").exists()          # pruned


def test_heal_sets_read_only_bits(tmp_path):
    repo = _make_repo(tmp_path)
    ws = tmp_path / "workspace"
    self_bundle.refresh_bundled_assets(_settings(tmp_path), repo_root=repo)

    assert stat.S_IMODE((ws / "self/bob/soul.md").stat().st_mode) == 0o444
    assert stat.S_IMODE((ws / "self/bob/avatar").stat().st_mode) == 0o555
    # user.md is the instance's own file — stays writable.
    assert stat.S_IMODE((ws / "user.md").stat().st_mode) == 0o644


def test_user_md_seeds_once_and_is_never_healed_over(tmp_path):
    repo = _make_repo(tmp_path)
    ws = tmp_path / "workspace"
    self_bundle.refresh_bundled_assets(_settings(tmp_path), repo_root=repo)

    (ws / "user.md").write_text("owner edited", encoding="utf-8")
    summary = self_bundle.refresh_bundled_assets(_settings(tmp_path), repo_root=repo)

    assert summary["seeded_user_md"] is False
    assert (ws / "user.md").read_text(encoding="utf-8") == "owner edited"


def test_missing_bundle_is_a_noop(tmp_path):
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    summary = self_bundle.refresh_bundled_assets(_settings(tmp_path), repo_root=empty)
    assert summary["personas"] == {}
    assert not (tmp_path / "workspace").exists()


def test_render_persona_from_workspace_files(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "self/bob").mkdir(parents=True)
    (ws / "self/bob/soul.md").write_text("SOUL BODY", encoding="utf-8")
    (ws / "self/bob/identity.md").write_text(
        "- **Name:** Bob\n- Host: {host}\n", encoding="utf-8")
    (ws / "self/bob/agents.md").write_text("AGENTS BODY", encoding="utf-8")
    (ws / "user.md").write_text("USER BODY", encoding="utf-8")

    rendered = render_persona(ws)
    assert "# Your Soul" in rendered and "SOUL BODY" in rendered
    assert "# Your Identity" in rendered and "- **Name:** Bob" in rendered
    assert "# Your Agents" in rendered and "AGENTS BODY" in rendered
    assert "# Your User" in rendered and "USER BODY" in rendered
    # {host} substituted with the real hostname
    assert "{host}" not in rendered
    assert platform.node() in rendered


def test_render_persona_survives_stray_braces(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "self/bob").mkdir(parents=True)
    (ws / "self/bob/identity.md").write_text(
        'json snippet {"a": 1} and {host}\n', encoding="utf-8")
    # No str.format anywhere in the render path — braces are literal text.
    rendered = render_persona(ws)
    assert '{"a": 1}' in rendered
    assert platform.node() in rendered


def test_render_persona_falls_back_to_the_repo_bundle(tmp_path):
    """Workspace missing the persona files entirely (heal not yet run):
    render still works from the bundle — this checkout has self/bob."""
    rendered = render_persona(tmp_path / "nonexistent-workspace")
    assert "# Your Soul" in rendered
    assert "My Face" in rendered  # the avatar manifest rides the identity
    assert "self/bob/avatar/reactions/bob-celebrate.mp4" in rendered
