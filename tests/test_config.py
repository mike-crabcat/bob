from __future__ import annotations

import os
from pathlib import Path

from cyborg.config import Settings


def _clear_cyborg_env(monkeypatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("CYBORG_"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / ".pytest-home"
    monkeypatch.setenv("HOME", str(home))


def test_settings_from_env_loads_cwd_dotenv(tmp_path: Path, monkeypatch) -> None:
    _clear_cyborg_env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "CYBORG_PORT=9011\n"
        "CYBORG_OPENCLAW_BASE_URL=https://openclaw.example\n"
        "CYBORG_OPENCLAW_TOKEN=secret\n",
        encoding="utf-8",
    )

    settings = Settings.from_env()

    assert settings.port == 9011
    assert settings.openclaw.base_url == "https://openclaw.example"
    assert settings.openclaw.token == "secret"
    assert settings.openclaw.resolved_gateway_url == "wss://openclaw.example"
    assert settings.openclaw.resolved_gateway_token == "secret"


def test_settings_from_env_loads_config_dir_dotenv(tmp_path: Path, monkeypatch) -> None:
    _clear_cyborg_env(monkeypatch, tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / ".env").write_text("CYBORG_PORT=9012\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"CYBORG_CONFIG_DIR={config_dir}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    settings = Settings.from_env()

    assert settings.config_dir == config_dir
    assert settings.port == 9012


def test_settings_from_env_prefers_existing_environment(tmp_path: Path, monkeypatch) -> None:
    _clear_cyborg_env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CYBORG_PORT=9013\n", encoding="utf-8")
    monkeypatch.setenv("CYBORG_PORT", "9014")

    settings = Settings.from_env()

    assert settings.port == 9014


def test_settings_from_env_supports_explicit_env_file(tmp_path: Path, monkeypatch) -> None:
    _clear_cyborg_env(monkeypatch, tmp_path)
    explicit_env = tmp_path / "custom.env"
    explicit_env.write_text("CYBORG_PORT=9015\n", encoding="utf-8")
    (tmp_path / ".env").write_text("CYBORG_PORT=9016\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CYBORG_ENV_FILE", str(explicit_env))

    settings = Settings.from_env()

    assert settings.port == 9015


def test_openclaw_gateway_settings_can_override_hook_defaults(tmp_path: Path, monkeypatch) -> None:
    _clear_cyborg_env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "CYBORG_OPENCLAW_BASE_URL=https://openclaw.example\n"
        "CYBORG_OPENCLAW_TOKEN=hook-secret\n"
        "CYBORG_OPENCLAW_GATEWAY_URL=ws://127.0.0.1:18789\n"
        "CYBORG_OPENCLAW_GATEWAY_TOKEN=gateway-secret\n",
        encoding="utf-8",
    )

    settings = Settings.from_env()

    assert settings.openclaw.resolved_gateway_url == "ws://127.0.0.1:18789"
    assert settings.openclaw.resolved_gateway_token == "gateway-secret"


def test_openclaw_gateway_only_settings_are_considered_enabled(tmp_path: Path, monkeypatch) -> None:
    _clear_cyborg_env(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CYBORG_OPENCLAW_GATEWAY_URL=ws://127.0.0.1:18789\n", encoding="utf-8")

    settings = Settings.from_env()

    assert settings.openclaw.enabled is True
    assert settings.openclaw.hooks_enabled is False
