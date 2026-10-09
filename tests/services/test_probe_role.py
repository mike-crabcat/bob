"""attention_probe role (models.yaml roles:) — independently selectable,
hot-reloaded, falls back to the passed default."""

from types import SimpleNamespace

from server.services.attention.tier2 import probe_model_for


def _ctx(tmp_path):
    return SimpleNamespace(settings=SimpleNamespace(config_dir=tmp_path))


def test_role_pins_probe_model_and_resolves_alias(tmp_path):
    (tmp_path / "models.yaml").write_text(
        "aliases:\n  luna: gpt-6-luna\nroles:\n  attention_probe: luna\n")
    assert probe_model_for(_ctx(tmp_path), "z-ai/glm-5.3-flash") == "gpt-6-luna"


def test_no_role_falls_back(tmp_path):
    (tmp_path / "models.yaml").write_text("aliases:\n  luna: gpt-6-luna\n")
    assert probe_model_for(_ctx(tmp_path), "z-ai/glm-5.3-flash") == "z-ai/glm-5.3-flash"
