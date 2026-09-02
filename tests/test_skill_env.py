"""Unit tests for build_skill_env venv and workspace injection."""

from __future__ import annotations

from pathlib import Path

from bob_server.services.skill_env import build_skill_env


def test_venv_dir_sets_virtual_env_and_prepends_path(tmp_path: Path) -> None:
    venv = tmp_path / "bobenv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")

    env = build_skill_env(base_env={"PATH": "/usr/bin:/bin"}, venv_dir=str(venv))

    assert env["VIRTUAL_ENV"] == str(venv)
    assert env["PATH"].split(":")[0] == str(venv / "bin")
    assert "/usr/bin:/bin" in env["PATH"]


def test_venv_dir_missing_python_leaves_path_untouched(tmp_path: Path) -> None:
    venv = tmp_path / "nope"
    original_path = "/usr/bin:/bin"

    env = build_skill_env(base_env={"PATH": original_path}, venv_dir=str(venv))

    assert "VIRTUAL_ENV" not in env
    assert env["PATH"] == original_path


def test_workspace_dir_still_injected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    env = build_skill_env(base_env={}, workspace_dir=str(workspace))

    assert env["BOB_WORKSPACE_DIR"] == str(workspace)


def test_venv_dir_clears_pythonhome(tmp_path: Path) -> None:
    venv = tmp_path / "bobenv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")

    env = build_skill_env(
        base_env={"PATH": "/usr/bin", "PYTHONHOME": "/should/be/cleared"},
        venv_dir=str(venv),
    )

    assert "PYTHONHOME" not in env
