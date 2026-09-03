"""Boot-time heal of bundled persona/avatar assets into the workspace.

The repo bundle (``self/<name>/`` persona dirs + ``user.md`` at the root) is
the single source of truth for the persona texts and the avatar pack. At
every startup the bundle is mirrored into the workspace: files whose content
differs are rewritten, extras are pruned, and read-only bits are set. A
persuaded-or-buggy agent can corrupt the workspace copies, but every restart
restores them — git history (not the retired persona_records revisions) is
the persona history.

``user.md`` is the one exception: seeded only-if-missing, then owned by the
instance (the owner profile is per-instance data, never healed over).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Advisory write-discouragement: the agent's own processes run as the same
# user and could chmod these back — the boot heal is the real enforcement.
# The healer re-grants write before rewriting anything it owns.
_FILE_MODE = 0o444
_DIR_MODE = 0o555


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_writable(path: Path, *, is_dir: bool) -> None:
    try:
        os.chmod(path, 0o755 if is_dir else 0o644)
    except OSError:
        pass


def _mirror_dir(src: Path, dst: Path) -> tuple[int, int]:
    """Mirror src into dst: copy content-changed files, delete extras.
    Returns (copied, removed) file counts. Unchanged files are left
    byte-untouched so mtimes (and the prompt cache keyed on them) stay
    stable across no-op heals."""
    copied = removed = 0
    dst.mkdir(parents=True, exist_ok=True)
    _make_writable(dst, is_dir=True)

    src_rels: set[Path] = set()
    for s in sorted(src.rglob("*")):
        rel = s.relative_to(src)
        src_rels.add(rel)
        d = dst / rel
        if s.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            _make_writable(d, is_dir=True)
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.is_symlink():
            d.unlink()
        elif d.is_file() and _file_hash(d) == _file_hash(s):
            continue
        if d.exists():
            _make_writable(d, is_dir=False)
        shutil.copyfile(s, d)
        copied += 1

    # Prune workspace paths the bundle no longer has (deepest first so
    # abandoned dirs empty out before their own removal).
    for d in sorted(dst.rglob("*"), reverse=True):
        if d.relative_to(dst) in src_rels:
            continue
        if d.is_dir() and not d.is_symlink():
            _make_writable(d, is_dir=True)
            try:
                d.rmdir()
            except OSError:
                pass
        else:
            if d.is_symlink() or d.is_file():
                _make_writable(d, is_dir=False)
                d.unlink(missing_ok=True)
                removed += 1

    # Re-apply the read-only bits to everything the bundle owns.
    for d in sorted(dst.rglob("*"), reverse=True):
        os.chmod(d, _DIR_MODE if d.is_dir() else _FILE_MODE)
    os.chmod(dst, _DIR_MODE)
    return copied, removed


def refresh_bundled_assets(settings: Any, *,
                           repo_root: Path | None = None) -> dict[str, Any]:
    """Mirror the bundled self/<name>/ dirs + user.md into the workspace.
    Called once per boot from the app lifespan. A missing bundle (dev
    checkout without self/) is a no-op. Returns a summary dict.
    ``repo_root`` overrides the bundle location (tests)."""
    repo_root = repo_root or Path(__file__).resolve().parents[2]
    bundle = repo_root / "self"
    workspace = Path(settings.harness.workspace_dir).expanduser()

    summary: dict[str, Any] = {"personas": {}, "seeded_user_md": False}
    if not bundle.is_dir():
        logger.info("no self/ bundle at %s — skipping heal", bundle)
        return summary

    # Each bundled persona dir heals independently (self/bob today; a future
    # self/<other>/ joins for free). Dirs in the workspace under self/ that
    # the bundle doesn't have are left alone — never prune another persona.
    for persona_dir in sorted(p for p in bundle.iterdir() if p.is_dir()):
        copied, removed = _mirror_dir(persona_dir, workspace / "self" / persona_dir.name)
        summary["personas"][persona_dir.name] = {"copied": copied, "removed": removed}
        if copied or removed:
            logger.info("healed self/%s: %d file(s) restored, %d pruned",
                        persona_dir.name, copied, removed)

    bundled_user = repo_root / "user.md"
    if bundled_user.is_file() and not (workspace / "user.md").exists():
        target = workspace / "user.md"
        target.write_text(bundled_user.read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(target, 0o644)  # the instance's own file — explicitly writable
        summary["seeded_user_md"] = True
        logger.info("seeded workspace/user.md (owner profile) from bundle")

    return summary
