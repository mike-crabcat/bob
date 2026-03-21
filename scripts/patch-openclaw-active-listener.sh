#!/usr/bin/env bash
set -euo pipefail

timestamp="${1:-$(date +%Y%m%d-%H%M%S)}"

files=(
  /usr/lib/node_modules/openclaw/dist/reply-Bm8VrLQh.js
  /usr/lib/node_modules/openclaw/dist/model-selection-46xMp11W.js
  /usr/lib/node_modules/openclaw/dist/model-selection-CU2b7bN6.js
  /usr/lib/node_modules/openclaw/dist/auth-profiles-DDVivXkv.js
  /usr/lib/node_modules/openclaw/dist/auth-profiles-DRjqKE3G.js
  /usr/lib/node_modules/openclaw/dist/discord-CcCLMjHw.js
)

echo "Applying OpenClaw active-listener hotfix"
echo "Backup suffix: $timestamp"

for file in "${files[@]}"; do
  if [[ ! -f "$file" ]]; then
    echo "Missing expected file: $file" >&2
    exit 1
  fi
done

for file in "${files[@]}"; do
  sudo cp -a "$file" "$file.bak.$timestamp"
done

for file in "${files[@]}"; do
  sudo sed -i \
    's|const listeners = /\* @__PURE__ \*/ new Map();|const listeners = globalThis.__activeWebListeners ??= new Map();|g' \
    "$file"
done

echo
echo "Verifying patched files..."
if rg -n 'const listeners = /\* @__PURE__ \*/ new Map\(\);' "${files[@]}"; then
  echo "Verification failed: old listener map declaration still present" >&2
  exit 1
fi

if ! rg -n '__activeWebListeners' "${files[@]}"; then
  echo "Verification failed: patched listener map declaration not found" >&2
  exit 1
fi

echo
echo "Restarting OpenClaw gateway..."
systemctl --user restart openclaw-gateway.service
systemctl --user status openclaw-gateway.service --no-pager

cat <<EOF

Patch applied.

Backups:
$(for file in "${files[@]}"; do printf '  %s.bak.%s\n' "$file" "$timestamp"; done)

Quick checks:
  openclaw channels status --probe
  openclaw gateway call send --json --params '{"channel":"whatsapp","to":"+61456224867","message":"direct test","idempotencyKey":"manual-test","agentId":"main","sessionKey":"agent:main:whatsapp:direct:+61456224867"}'

Rollback:
  for f in "${files[@]}"; do sudo cp -a "\$f.bak.$timestamp" "\$f"; done
  systemctl --user restart openclaw-gateway.service
EOF
