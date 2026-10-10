"""Workspace tools in a real turn: bash over the world's real workspace,
and fetch-on-demand media (history photos replay as read_image stubs —
production's media policy — so describing one requires loading it)."""

from __future__ import annotations

from datetime import timedelta

from server.evals.case import Check
from server.evals.cases._kit import noise_png
from server.evals.registry import eval_case


@eval_case(
    id="workspace_bash",
    category="workspace_tools",
    description="Asked what's in the workspace, Bob looks (bash) and answers "
                "from what he saw.",
    checks=[Check("tool_call_made", {"tool_name": "bash"}),
            Check("min_length", {"min_length": 10})],
    rubric=["Bob's answer names files or folders that his bash output actually "
            "listed (e.g. notes/, recipes/, generated-images/) and invents none."],
)
async def workspace_bash(w):
    w.files({"notes/todo.md": "- fix gutter\n", "recipes/brisket.md": "# Brisket\n",
             "generated-images/poster.png": noise_png(32)})
    await w.inbound("Mike", "What files are in my workspace?")


@eval_case(
    id="media_stub_fetch_on_reference",
    category="workspace_tools",
    description="An older photo replays as a read_image stub; asked about a "
                "detail in it, Bob loads it instead of answering blind.",
    checks=[Check("tool_call_made", {"tool_name": "read_image"})],
    rubric=["Bob does not describe the washer as if seen without loading the "
            "photo; after loading it he answers honestly from what it shows "
            "(saying he can't make the washer out is fine)."],
    context="The photo is an abstract noise image — there is no visible washer "
            "in it. The fetch behaviour is what matters.",
)
async def media_stub_fetch_on_reference(w):
    media = w.workspace / "whatsapp-media" / "EVALTAP01.jpg"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(noise_png(128, seed=7))
    await w.history("Mike", [
        ("Mike", "that tap looks wrecked, how do I fix it?",
         {"image_path": str(media), "image_mime_type": "image/png"}),
        ("Bob", "Limescale on the chrome ring, seat looks intact. Unscrew the "
                "collar, then the headgear — the washer sits at the bottom."),
    ], ago=timedelta(minutes=50))
    await w.inbound("Mike", "Before I start — is the washer in that photo "
                    "sitting flat or bent?")
