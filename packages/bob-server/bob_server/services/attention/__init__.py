"""Attention — the Bob3 turn coordinator (Phase III).

LIVE since the Phase III cutover (soak skipped by operator decision):
AttentionCoordinator owns WhatsApp dispatch timing (Tier 1 windows) and
unaddressed-group gating (Tier 2 probe). attention_shadow is the decision
audit trail. `record_shadow_decision` remains for channels not yet cut over
(email records its always-addressed ACT there).
"""

from bob_server.services.attention.tier0 import AddressedResult, detect_addressed
from bob_server.services.attention.shadow import record_shadow_decision
from bob_server.services.attention.coordinator import AttentionCoordinator

__all__ = ["AddressedResult", "detect_addressed", "record_shadow_decision",
           "AttentionCoordinator"]
