#!/usr/bin/env bash
# Instance bootstrap. Root path (default when started by compose without a
# `user:` override): pin the runtime uid/gid (BOB_UID/BOB_GID, default 1000)
# to match the host user owning the bind mounts, fix the named-volume roots,
# then re-exec unprivileged. Unprivileged path: seed core skills into the
# workspace (if missing) and provision the harness venv (bobenv).
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
  uid="${BOB_UID:-1000}"
  gid="${BOB_GID:-$uid}"
  if ! getent passwd "$uid" >/dev/null 2>&1; then
    useradd -u "$uid" -o -g "$gid" -d /home/bob -s /bin/bash bob 2>/dev/null || true
  fi
  chown "$uid:$gid" /home/bob 2>/dev/null || true
  # Named volumes inherit the image's ownership on first use — realign them
  # when running under a different uid. (Never touches the data bind mount.)
  for d in /home/bob/.claude /home/bob/bobenv; do
    if [ -d "$d" ]; then chown -R "$uid:$gid" "$d" 2>/dev/null || true; fi
  done
  exec setpriv --reuid="$uid" --regid="$gid" --clear-groups -- /entrypoint.sh "$@"
fi

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
WANT="$(sha256sum /app/skills/requirements.txt 2>/dev/null | cut -d' ' -f1 || true)"
if [ -n "$WANT" ] && [ "$(cat "$STAMP" 2>/dev/null || true)" != "$WANT" ]; then
  if [ ! -x "$VENV/bin/python" ]; then
    echo "[entrypoint] creating harness venv at $VENV"
    python -m venv "$VENV"
  fi
  echo "[entrypoint] installing skill requirements into bobenv"
  "$VENV/bin/pip" install --quiet --disable-pip-version-check -r /app/skills/requirements.txt
  echo "$WANT" > "$STAMP"
fi

exec "$@"
