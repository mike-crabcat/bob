#!/usr/bin/env bash
# Promote curated skills from the live workspace into the repo bundle (skills/).
# Manual-by-design: the workspace is the agent's runtime lab — nothing
# auto-promotes into distribution. Run this, review the diff-stat, commit.
#
# Rules enforced here:
#   - explicit allowlist only (the confirmed ship list)
#   - per-skill junk/sample excludes
#   - hard gate: refuses to leave any secret-shaped file in the bundle
#
# Usage: scripts/capture-skills.sh [--check]   (--check: report drift, change nothing)
set -euo pipefail
cd "$(dirname "$0")/.."

SHIP=(
  browser
  skill-guru
  docx-to-md
  md-to-docx
  pdf-to-text
  itinerary-pdf
  creative-writing-review
  changelog-impact
  openai-image
  google-places
  videogen
)

# Per-skill excludes beyond the universal ones (samples, scratch, secrets).
excludes_for() {
  case "$1" in
    docx-to-md) echo " --exclude the-harvestman-v1.docx --exclude the-harvestman-v1.md" ;;
    md-to-docx) echo " --exclude test-example.docx --exclude test-example.md --exclude UPGRADE-SUMMARY.md --exclude validate_docx.py" ;;
    pdf-to-text) echo " --exclude trips" ;;
    *) echo "" ;;
  esac
}

UNIVERSAL="--exclude __pycache__ --exclude *.pyc --exclude .venv"

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

WS="${BOB_WORKSPACE_DIR:-$HOME/workspace}"

for s in "${SHIP[@]}"; do
  src="$WS/skills/$s"
  [[ -d "$src" ]] || { echo "ERROR: workspace skill missing: $src" >&2; exit 1; }
  if [[ $CHECK -eq 1 ]]; then
    rsync -a --delete --itemize-changes --dry-run $UNIVERSAL $(excludes_for "$s") \
      --exclude api_key --exclude apikey --exclude '*_token' --exclude .env \
      "$src/" "skills/$s/" | grep -q '^[<>f]' && echo "DRIFT: $s" || true
  else
    mkdir -p "skills/$s"
    # shellcheck disable=SC2086
    rsync -a --delete $UNIVERSAL $(excludes_for "$s") \
      --exclude api_key --exclude apikey --exclude '*_token' --exclude .env \
      "$src/" "skills/$s/"
  fi
done

[[ $CHECK -eq 1 ]] && exit 0

# Hard gate: no secret-shaped filenames may live in the bundle.
if find skills/ \( -iname '*key*' -o -iname '*token*' -o -name '.env' \) -type f | grep -q .; then
  echo "ERROR: secret-shaped file in bundle — reverting skills/ and aborting:" >&2
  find skills/ \( -iname '*key*' -o -iname '*token*' -o -name '.env' \) -type f >&2
  git checkout -- skills/ 2>/dev/null || true
  exit 1
fi

echo "captured ${#SHIP[@]} skills — review and commit:"
git diff --stat -- skills/ | tail -3
git status --porcelain -- skills/ | head -5
