#!/usr/bin/env bash
set -euo pipefail

# Roll out a new cyborg-server release:
#   1. Bump the patch version
#   2. Build & publish the wheel to PyPI
#   3. Sync the skill to ~/.openclaw and pin the new version

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SERVER_TOML="packages/cyborg-server/pyproject.toml"
SKILL_SRC="openclaw-skill"
SKILL_DST="$HOME/.openclaw/workspace/skills/cyborg-cli"
DIST="packages/cyborg-server/dist"
TOKEN="${PYPI_TOKEN:?Set PYPI_TOKEN env var before running}"

# ── 1. Bump version ──────────────────────────────────────────────────────
CURRENT=$(grep '^version = ' "$SERVER_TOML" | head -1 | sed 's/version = "\(.*\)"/\1/')
IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT"
PATCH=$((PATCH + 1))
NEW_VERSION="$MAJOR.$MINOR.$PATCH"

echo "Bumping version: $CURRENT -> $NEW_VERSION"
sed -i "s/^version = \"$CURRENT\"/version = \"$NEW_VERSION\"/" "$SERVER_TOML"

# ── 2. Build ─────────────────────────────────────────────────────────────
echo "Building wheel..."
rm -rf "$DIST"
uv build packages/cyborg-server -o "$DIST"

# ── 3. Publish ───────────────────────────────────────────────────────────
echo "Publishing $NEW_VERSION to PyPI..."
uv tool run twine upload --repository pypi -u __token__ -p "$TOKEN" "$DIST/"*

# ── 4. Pin version in skill pyproject.toml files ─────────────────────────
echo "Pinning cyborg-server>=$NEW_VERSION in skill pyproject.toml files"
for toml in "$SKILL_SRC/pyproject.toml" "$SKILL_DST/pyproject.toml"; do
    sed -i "s/cyborg-server.*/cyborg-server>=$NEW_VERSION\",/" "$toml"
done

# ── 5. Sync skill to openclaw ────────────────────────────────────────────
echo "Syncing skill to $SKILL_DST"
rsync -a --delete "$SKILL_SRC/" "$SKILL_DST/"

echo ""
echo "Done. cyborg-server $NEW_VERSION published and skill synced."
