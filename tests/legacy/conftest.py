"""Legacy test quarantine — NOT collected by the suite.

These tests date from the pre-OpenRouter architecture (`openclaw` config
symbols, projects/tasks-era autonomy) and rotted in place: the deploy gate
only ever ran the bob-server package suite, so nothing noticed. Quarantined
during the 2026-09 docker restructure rather than deleted — review and
salvage or drop. Kept out of collection so the deploy gate stays meaningful.
"""

collect_ignore_glob = ["*.py", "openclaw_acceptance", "mocks"]
