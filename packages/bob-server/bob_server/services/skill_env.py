"""Environment variable mapping for skill subprocesses.

Maps BOB_-prefixed config env vars to the standard names that
third-party SDKs and tools expect.
"""

from __future__ import annotations

import os

# Mapping: BOB_ env var -> standard env var to inject
ENV_MAPPINGS: dict[str, str] = {
    "BOB_OPENAI_API_KEY": "OPENAI_API_KEY",
    "BOB_OPENAI_BASE_URL": "OPENAI_BASE_URL",
    "BOB_AGENTMAIL_API_KEY": "AGENTMAIL_API_KEY",
    "BOB_GOOGLE_PLACES_API_KEY": "GOOGLE_PLACES_API_KEY",
    "BOB_GIPHY_API_KEY": "GIPHY_API_KEY",
}


def build_skill_env(
    base_env: dict[str, str] | None = None,
    *,
    workspace_dir: str | None = None,
) -> dict[str, str]:
    """Build the environment dict for a skill subprocess.

    Starts from the parent process environment (or base_env if provided),
    then adds standard-name aliases for any configured BOB_ secrets.
    Also injects BOB_WORKSPACE_DIR when workspace_dir is provided.
    Only injects a mapping if the BOB_ var is set and non-empty.
    """
    env = dict(base_env or os.environ)
    for bob_key, standard_key in ENV_MAPPINGS.items():
        value = env.get(bob_key, "")
        if value:
            env[standard_key] = value
    if workspace_dir:
        env["BOB_WORKSPACE_DIR"] = workspace_dir
    return env
