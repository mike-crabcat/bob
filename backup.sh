#!/bin/bash
# Backup script for Cyborg project

set -e

BACKUP_DIR="$HOME/.openclaw/backups/cyborg"
SOURCE_DIR="$HOME/.openclaw/workspace/projects/cyborg"
DB_DIR="$HOME/.local/share/cyborg"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="cyborg_backup_${TIMESTAMP}"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"

echo "Creating Cyborg backup: $BACKUP_NAME"

# Create backup directory
mkdir -p "$BACKUP_PATH"

# Backup source code
echo "  → Backing up source code..."
cp -r "$SOURCE_DIR" "$BACKUP_PATH/source"

# Backup database
echo "  → Backing up database..."
if [ -d "$DB_DIR" ]; then
    mkdir -p "$BACKUP_PATH/database"
    cp -r "$DB_DIR"/* "$BACKUP_PATH/database/" 2>/dev/null || true
fi

# Create tarball
echo "  → Creating tarball..."
cd "$BACKUP_DIR"
tar -czf "${BACKUP_NAME}.tar.gz" "$BACKUP_NAME"
rm -rf "$BACKUP_NAME"

# Keep only last 10 backups
echo "  → Cleaning old backups..."
ls -t *.tar.gz 2>/dev/null | tail -n +11 | xargs -r rm -f

echo "✅ Backup complete: $BACKUP_DIR/${BACKUP_NAME}.tar.gz"
echo ""
echo "Recent backups:"
ls -lh "$BACKUP_DIR"/*.tar.gz 2>/dev/null | tail -5
