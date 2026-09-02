#!/usr/bin/env bash
# Instance bootstrap: seed core skills into the workspace (if missing) and
# provision the harness venv (bobenv) with skill requirements. Idempotent —
# the agent's live, evolved copies are never overwritten on re-boot.
set -euo pipefail

WS="${BOB_HARNESS_WORKSPACE_DIR:-$HOME/workspace}"
VENV="${BOB_HARNESS_VENV_DIR:-$HOME/bobenv}"

# 1. Seed skills per-skill if missing (skel semantics).
if [ -d /app/skills ]; then
  mkdir -p "$WS/skills"
  for d in /app/skills/*/; do
    name="$(basename "$d")"
    if [ ! -d "$WS/skills/$name" ]; then
      echo "[entrypoint] seeding skill: $name"
      cp -r "$d" "$WS/skills/$name"
    fi
  done
fi

# 2. bobenv + skill pip requirements, stamped by content hash.
STAMP="$VENV/.skills-requirements.sha"
WANT="$(cat /app/skills/requirements.txt 2>/dev/null | sha256sum | cut -d' ' -f1 || true)"
if [ -n "$WANT" ] && [ "$(cat "$STAMP" 2>/dev/null || true)" != "$WANT" ]; then
  if [ ! -x "$VENV/bin/python" ]; then
    echo "[entrypoint] creating harness venv at $VENV"
    python -m venv "$VENV"
  fi
  echo "[entrypoint] installing skill requirements into bobenv"
  "$VENV/bin/pip" install --quiet --disable-pip-version-check -r /app/skills/requirements.txt
  mkdir -p "$VENV" && echo "$WANT" > "$STAMP"
fi

exec "$@"
