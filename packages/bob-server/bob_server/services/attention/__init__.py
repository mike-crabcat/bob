"""Attention — the Bob3 turn coordinator (Phase III).

Currently SHADOW MODE ONLY: the legacy dispatcher (patience gate) stays
authoritative; this package computes what the Attention coordinator WOULD
do and records it to attention_shadow for agreement measurement. Cutover is
gated on the plan's SLOs (≥90% ACT agreement over a soak week).
"""

from bob_server.services.attention.tier0 import AddressedResult, detect_addressed
from bob_server.services.attention.shadow import record_shadow_decision

__all__ = ["AddressedResult", "detect_addressed", "record_shadow_decision"]
