"""Dream system — reflective self-improvement (resolutions) + proactive plans.

Design: dream-v2-plan.md at the repo root. Two artifact types:
- resolutions: evidence-cited self-improvement items, kept when verified
- plans: unfinished business detected in conversation, announced in the
  session where the evidence was cited

All LLM passes run on the memory model. Dreams never block the heartbeat.
"""

from bob_server.services.dream.announce import AnnounceService
from bob_server.services.dream.journal import JournalService
from bob_server.services.dream.prospective import ProspectiveService
from bob_server.services.dream.review import ReviewService
from bob_server.services.dream.runner import DreamRunner
from bob_server.services.dream.store import DreamStore

__all__ = [
    "AnnounceService",
    "DreamRunner",
    "DreamStore",
    "JournalService",
    "ProspectiveService",
    "ReviewService",
]
