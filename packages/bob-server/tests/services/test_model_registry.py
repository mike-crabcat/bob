"""Tests for services/model_registry.py — alias resolution, provider routing,
pricing, and yaml hot-reload."""

from __future__ import annotations

from bob_server.services import model_registry

import pytest


YAML = """\
aliases:
  cheap: gpt-5.6-luna
  Chinese: z-ai/glm-5.3-flash
pricing:
  z-ai/glm-5.3-flash: [0.075, 0.25]
"""


@pytest.fixture()
def cfg(tmp_path):
    (tmp_path / "models.yaml").write_text(YAML, encoding="utf-8")
    model_registry._models_cache = None
    yield tmp_path
    model_registry._models_cache = None


def test_provider_for():
    assert model_registry.provider_for("gpt-5.6-sol") == "openai"
    assert model_registry.provider_for("z-ai/glm-5.3-flash") == "openrouter"
    assert model_registry.provider_for("") == "openai"


def test_aliases_case_insensitive(cfg):
    assert model_registry.resolve("cheap", cfg) == "gpt-5.6-luna"
    assert model_registry.resolve("CHEAP", cfg) == "gpt-5.6-luna"
    assert model_registry.resolve("Chinese", cfg) == "z-ai/glm-5.3-flash"


def test_resolve_unknown_passthrough(cfg):
    assert model_registry.resolve("gpt-5.6-terra", cfg) == "gpt-5.6-terra"
    assert model_registry.resolve("z-ai/glm-5.3", cfg) == "z-ai/glm-5.3"


def test_validate_known_alias(cfg):
    ok, target = model_registry.validate("chinese", cfg, openrouter_enabled=True)
    assert ok and target == "z-ai/glm-5.3-flash"
    # Alias needing OpenRouter is rejected when the provider is unconfigured.
    ok, msg = model_registry.validate("chinese", cfg, openrouter_enabled=False)
    assert not ok and "OpenRouter" in msg


def test_validate_slugs(cfg):
    ok, target = model_registry.validate("openai/gpt-5.6-sol", cfg, openrouter_enabled=True)
    assert ok and target == "openai/gpt-5.6-sol"
    ok, msg = model_registry.validate("z-ai/glm-5.3-flash", cfg, openrouter_enabled=False)
    assert not ok and "not configured" in msg
    ok, target = model_registry.validate("gpt-5.6-terra", cfg, openrouter_enabled=False)
    assert ok and target == "gpt-5.6-terra"
    ok, msg = model_registry.validate("not a model!", cfg, openrouter_enabled=True)
    assert not ok


def test_missing_file_means_no_aliases(tmp_path):
    model_registry._models_cache = None
    try:
        assert model_registry.aliases(tmp_path) == {}
        assert model_registry.pricing(tmp_path) == {}
        assert model_registry.resolve("anything", tmp_path) == "anything"
    finally:
        model_registry._models_cache = None


def test_malformed_yaml_is_tolerated(tmp_path):
    (tmp_path / "models.yaml").write_text("aliases: [broken\n  - {", encoding="utf-8")
    model_registry._models_cache = None
    try:
        assert model_registry.aliases(tmp_path) == {}
    finally:
        model_registry._models_cache = None


def test_pricing_load(cfg):
    assert model_registry.pricing(cfg) == {"z-ai/glm-5.3-flash": (0.075, 0.25)}


def test_mtime_reload(cfg):
    assert model_registry.resolve("cheap", cfg) == "gpt-5.6-luna"
    path = cfg / "models.yaml"
    path.write_text(
        "aliases:\n  cheap: gpt-5.6-terra\npricing:\n  z-ai/glm-5.3-flash: [0.075, 0.25]\n",
        encoding="utf-8")
    # Same mtime granularity risk on fast filesystems — nudge mtime explicitly.
    st = path.stat()
    import os
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert model_registry.resolve("cheap", cfg) == "gpt-5.6-terra"
    assert model_registry.pricing(cfg) == {"z-ai/glm-5.3-flash": (0.075, 0.25)}
