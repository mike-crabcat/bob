#!/usr/bin/env bash
set -euo pipefail

TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)
BACKUP_NAME="bob_backup_${TIMESTAMP}.zip"
BACKUP_PATH="$HOME/${BACKUP_NAME}"
CONFIG_DIR="$HOME/config"
DATA_DIR="$HOME/data"
WORKSPACE_DIR="$HOME/workspace"

# Collect files into a temp staging dir
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# Databases (grab WAL/SHM alongside bob.db so the snapshot is crash-consistent)
for db in \
  "$DATA_DIR/bob.db" \
  "$DATA_DIR/bob.db-wal" \
  "$DATA_DIR/bob.db-shm" \
  "$DATA_DIR/whatsappbridge/.env"; do
  if [ -f "$db" ]; then
    dest="$STAGE/$(echo "$db" | sed "s|^$HOME/||")"
    mkdir -p "$(dirname "$dest")"
    cp "$db" "$dest"
  fi
done

# Config directory
if [ -d "$CONFIG_DIR" ]; then
  mkdir -p "$STAGE/config"
  cp -r "$CONFIG_DIR"/.env "$STAGE/config/" 2>/dev/null || true
  for f in "$CONFIG_DIR"/settings*.json; do
    [ -f "$f" ] || continue
    dest="$STAGE/$(echo "$f" | sed "s|^$HOME/||")"
    mkdir -p "$(dirname "$dest")"
    cp "$f" "$dest"
  done
fi

# Data directory (non-code runtime data)
if [ -d "$DATA_DIR" ]; then
  rsync -a --exclude='*.log' --exclude='backups/' "$DATA_DIR/" "$STAGE/data/" 2>/dev/null || \
    cp -r "$DATA_DIR" "$STAGE/data/"
fi

# Workspace directory
if [ -d "$WORKSPACE_DIR" ]; then
  rsync -a --exclude='*.log' "$WORKSPACE_DIR/" "$STAGE/workspace/" 2>/dev/null || \
    cp -r "$WORKSPACE_DIR" "$STAGE/workspace/"
fi

# Project-level config files (not source code)
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
for f in \
  "$PROJECT_DIR/.env" \
  "$PROJECT_DIR/.env.local" \
  "$PROJECT_DIR/pyproject.toml" \
  "$PROJECT_DIR/DOCS.yaml" \
  "$PROJECT_DIR/packages/bob-server/pyproject.toml"; do
  [ -f "$f" ] || continue
  dest="$STAGE/$(echo "$f" | sed "s|^$HOME/||")"
  mkdir -p "$(dirname "$dest")"
  cp "$f" "$dest"
done

# Zip it up
cd "$STAGE"
zip -r "$BACKUP_PATH" .
cd - > /dev/null

echo "Backup created: ${BACKUP_PATH}"
echo "Size: $(du -h "$BACKUP_PATH" | cut -f1)"
