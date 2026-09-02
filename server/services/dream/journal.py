"""Journal synthesis: run facts → narrative journal_text (memory model)."""

from __future__ import annotations

import json
from typing import Any

from server.context import AppContext
from server.services.base import BaseService


class JournalService(BaseService):
    async def synthesise(self, *, facts: dict[str, Any]) -> str:
        """Run facts → markdown narrative. Prose output, not JSON."""
        from server.services.dream.prompts import SYNTHESIS_SYSTEM
        from server.services.llm_dispatch import LLMDispatchService

        user_prompt = "Run facts (JSON):\n" + json.dumps(facts, indent=1, default=str)[:12000]
        llm = LLMDispatchService(self.ctx)
        response = await llm.chat(
            messages=[
                {"role": "system", "content": SYNTHESIS_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            call_category="dream_synthesis",
            model=self.ctx.settings.openai.get_memory_model(),
            temperature=0.4,
        )
        return (response or "").strip() or "_(journal synthesis unavailable)_"
