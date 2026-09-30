#!/usr/bin/env bash
# One-time (idempotent) setup for the bambu-x2d-prep skill.
# - render venv (.venv: numpy + matplotlib for render_views.py)
# - Bambu Studio CLI AppImage, extracted into workspace/tools/bambu-studio
set -euo pipefail
skill="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -x "$skill/.venv/bin/python" ]; then
  echo "[bootstrap] creating render venv"
  python3 -m venv "$skill/.venv"
fi
"$skill/.venv/bin/pip" install --quiet --disable-pip-version-check matplotlib numpy
echo "[bootstrap] render venv ok"

STUDIO_DIR="${BOB_WORKSPACE:-$skill/../..}/tools/bambu-studio"
APPIMAGE="$STUDIO_DIR/BambuStudio.AppImage"
EXTRACTED="$STUDIO_DIR/squashfs-root"
URL="https://github.com/bambulab/BambuStudio/releases/download/v02.08.02.61/BambuStudio_ubuntu24.04-v02.08.02.61-20260820225108.AppImage"

if [ ! -x "$EXTRACTED/AppRun" ]; then
  mkdir -p "$STUDIO_DIR"
  if [ ! -f "$APPIMAGE" ]; then
    echo "[bootstrap] downloading Bambu Studio v02.08.02.61 (~230MB, one time)"
    curl -sL -o "$APPIMAGE" "$URL"
  fi
  chmod +x "$APPIMAGE"
  echo "[bootstrap] extracting AppImage"
  (cd "$STUDIO_DIR" && ./BambuStudio.AppImage --appimage-extract >/dev/null)
fi
echo "[bootstrap] studio: $EXTRACTED/AppRun"
echo "[bootstrap] done"
