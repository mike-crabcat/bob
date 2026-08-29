"""Per-provider circuit-breaker isolation + OpenRouter quota classification."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bob_server.services import quota_gate
from bob_server.services.dispatch_runner import _is_quota_error


@pytest.fixture(autouse=True)
def _clean_gate():
    quota_gate.reset()
    yield
    quota_gate.reset()


class TestPerProviderGate:
    def test_openrouter_failure_does_not_block_openai(self):
        assert quota_gate.record_failure(
            RuntimeError("OpenRouter error: insufficient credits"), "openrouter")
        with pytest.raises(quota_gate.QuotaExhaustedError):
            quota_gate.check("openrouter")
        quota_gate.check("openai")  # must not raise

    def test_openai_failure_does_not_block_openrouter(self):
        assert quota_gate.record_failure(
            RuntimeError("OpenAI API error: insufficient_quota"), "openai")
        with pytest.raises(quota_gate.QuotaExhaustedError):
            quota_gate.check("openai")
        quota_gate.check("openrouter")

    def test_success_closes_only_that_provider(self):
        quota_gate.record_failure(RuntimeError("insufficient_quota"), "openai")
        quota_gate.record_success("openai")
        quota_gate.check("openai")
        quota_gate.record_failure(RuntimeError("insufficient_quota"), "openrouter")
        quota_gate.record_success("openai")  # no-op for openrouter
        with pytest.raises(quota_gate.QuotaExhaustedError):
            quota_gate.check("openrouter")

    def test_status_keeps_flat_keys_for_ops_tile(self):
        quota_gate.record_failure(RuntimeError("insufficient_quota"), "openai")
        s = quota_gate.status()
        assert s["open"] is True
        assert s["remaining_s"] > 0
        assert "openai" in s["providers"]
        assert s["providers"]["openai"]["open"] is True

    def test_non_quota_failure_never_opens(self):
        assert quota_gate.record_failure(RuntimeError("404 model not found"), "openrouter") is False
        quota_gate.check("openrouter")


class TestIsQuotaError:
    def test_openai_patterns(self):
        assert _is_quota_error(RuntimeError("insufficient_quota for plan"))
        assert _is_quota_error(RuntimeError("429 quota exceeded"))
        assert _is_quota_error(RuntimeError("credit_balance_exhausted"))

    def test_openrouter_patterns(self):
        assert _is_quota_error(RuntimeError("OpenRouter: insufficient credits for this request"))
        assert _is_quota_error(RuntimeError("402 Payment Required: not enough credits"))
        assert _is_quota_error(RuntimeError("Provider returned error: Not enough credits"))

    def test_non_quota_errors_stay_false(self):
        assert not _is_quota_error(RuntimeError("429 rate limit exceeded, retry later"))
        assert not _is_quota_error(RuntimeError("404 model not found"))
        assert not _is_quota_error(RuntimeError("connection reset by peer"))


class TestResolveModelOverride:
    @pytest.mark.asyncio
    async def test_resolves_alias_and_respects_unconfigured_openrouter(self, db, tmp_path):
        from bob_server.services import model_registry
        from bob_server.services.dispatch_runner import DispatchRunner
        from bob_server.repositories.conversations import ConversationRepository

        (tmp_path / "models.yaml").write_text(
            "aliases:\n  chinese: z-ai/glm-5.3-flash\n", encoding="utf-8")
        model_registry._models_cache = None
        try:
            repo = ConversationRepository(db)
            await repo.ensure("wa:123")
            await repo.set_policy("wa:123", {"model_override": "chinese"})

            ctx_on = SimpleNamespace(db=db, settings=SimpleNamespace(
                config_dir=tmp_path, openrouter=SimpleNamespace(enabled=True)))
            assert await DispatchRunner(ctx_on)._resolve_model_override("wa:123") == "z-ai/glm-5.3-flash"

            ctx_off = SimpleNamespace(db=db, settings=SimpleNamespace(
                config_dir=tmp_path, openrouter=SimpleNamespace(enabled=False)))
            assert await DispatchRunner(ctx_off)._resolve_model_override("wa:123") is None
        finally:
            model_registry._models_cache = None

    @pytest.mark.asyncio
    async def test_unset_override_returns_none(self, db, tmp_path):
        from bob_server.services.dispatch_runner import DispatchRunner
        from bob_server.repositories.conversations import ConversationRepository
        await ConversationRepository(db).ensure("wa:456")
        ctx = SimpleNamespace(db=db, settings=SimpleNamespace(
            config_dir=tmp_path, openrouter=SimpleNamespace(enabled=True)))
        assert await DispatchRunner(ctx)._resolve_model_override("wa:456") is None
