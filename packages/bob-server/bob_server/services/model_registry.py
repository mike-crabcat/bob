"""Model registry — alias resolution and provider routing for model slugs.

One vocabulary table for "which provider serves this model" (mirrors the
single-vocabulary-table convention noted in subagent_service.py). Slugs
containing "/" follow OpenRouter's vendor/model convention
(e.g. ``z-ai/glm-5.3-flash``) and route via OpenRouter; plain names
(``gpt-5.6-sol``) route via direct OpenAI.

Aliases and per-model pricing live in ``{config_dir}/models.yaml``,
hot-reloaded on mtime/size change (the skill_loader.py pattern — Settings
is a startup singleton, so from_env()-time loading alone would need a
restart):

    aliases:
      cheap: gpt-5.6-luna
      chinese: z-ai/glm-5.3-flash
    pricing:            # USD per 1M tokens: [input, output]
      z-ai/glm-5.3-flash: [0.075, 0.25]
    effort:             # reasoning-effort hint, applied when a caller pins none
      z-ai/glm-5.3-flash: medium
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

PROVIDER_OPENAI = "openai"
PROVIDER_OPENROUTER = "openrouter"

_PLAIN_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

# Module-level cache keyed by (mtime, size).
_models_cache: tuple[tuple[float, int], dict[str, Any]] | None = None


def provider_for(model: str) -> str:
    """OpenRouter serves vendor-qualified slugs; everything else is OpenAI."""
    return PROVIDER_OPENROUTER if "/" in model else PROVIDER_OPENAI


def _load_models(config_dir: Path) -> dict[str, Any]:
    global _models_cache
    path = Path(config_dir).expanduser() / "models.yaml"
    try:
        stat = path.stat()
        fingerprint = (stat.st_mtime, stat.st_size)
    except OSError:
        _models_cache = None
        return {}
    if _models_cache is not None and _models_cache[0] == fingerprint:
        return _models_cache[1]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        logger.warning("models.yaml unreadable at %s — aliases/pricing ignored", path, exc_info=True)
        data = {}
    if not isinstance(data, dict):
        logger.warning("models.yaml root is not a mapping — ignoring")
        data = {}
    _models_cache = (fingerprint, data)
    return data


def aliases(config_dir: Path) -> dict[str, str]:
    """Alias → model slug, keys lowercased for case-insensitive lookup."""
    raw = _load_models(config_dir).get("aliases") or {}
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for name, slug in raw.items():
            if (isinstance(name, str) and isinstance(slug, str)
                    and name.strip() and slug.strip()):
                out[name.strip().lower()] = slug.strip()
    return out


def pricing(config_dir: Path) -> dict[str, tuple[float, float]]:
    """Model slug → (input, output) USD per 1M tokens."""
    raw = _load_models(config_dir).get("pricing") or {}
    out: dict[str, tuple[float, float]] = {}
    if isinstance(raw, dict):
        for slug, rates in raw.items():
            if isinstance(slug, str) and isinstance(rates, (list, tuple)) and len(rates) == 2:
                try:
                    out[slug.strip()] = (float(rates[0]), float(rates[1]))
                except (TypeError, ValueError):
                    logger.warning("models.yaml pricing for %s is not numeric — ignored", slug)
    return out


# Reasoning-effort levels the OpenAI Responses API and OpenRouter's gateway
# both understand. Anything else in models.yaml is a typo that would 400
# every main turn, so it's warned about and ignored instead.
_VALID_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})


def effort_defaults(config_dir: Path) -> dict[str, str]:
    """Model slug → reasoning-effort hint, from models.yaml's ``effort:`` map.

    Applied by openai_service when a caller doesn't pin its own effort
    (main turns pin none): a thinking model at default effort reasons at full
    budget — ~95% of GLM-5.3-flash output tokens — which dominates turn
    latency. Levels are normalised lowercased; unknown values are ignored.
    """
    raw = _load_models(config_dir).get("effort") or {}
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for slug, effort in raw.items():
            if not (isinstance(slug, str) and isinstance(effort, str)):
                continue
            level = effort.strip().lower()
            if level not in _VALID_EFFORTS:
                logger.warning("models.yaml effort for %s is not a known level (%r) — ignored", slug, effort)
                continue
            out[slug.strip()] = level
    return out


def resolve(name: str, config_dir: Path) -> str:
    """Resolve an alias (case-insensitive) to its slug; unknown names pass
    through unchanged so callers can pass already-resolved slugs."""
    name = name.strip()
    return aliases(config_dir).get(name.lower(), name)


def validate(
    name: str, config_dir: Path, *, openrouter_enabled: bool,
) -> tuple[bool, str]:
    """Validate a /model target. Returns ``(ok, message)`` — on success the
    message is the resolved slug; on failure it's a user-facing reason."""
    name = name.strip()
    if not name:
        return False, "no model name given"
    alias_map = aliases(config_dir)
    target = alias_map.get(name.lower())
    if target is not None:
        if provider_for(target) == PROVIDER_OPENROUTER and not openrouter_enabled:
            return False, f"alias '{name}' points at {target}, which needs OpenRouter — no API key configured"
        return True, target
    if "/" in name:
        if not openrouter_enabled:
            return False, f"{name} is an OpenRouter model but OpenRouter is not configured (no API key)"
        return True, name
    if not _PLAIN_MODEL_RE.match(name):
        return False, f"'{name}' is not a valid model name"
    return True, name
