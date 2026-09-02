#!/usr/bin/env bash
# Deploy gate: the test suite (including SQL-ownership enforcement) must be
# green before anything reaches the live service. Usage:
#   ./deploy.sh            # test -> build UI -> push -> restart -> healthcheck
set -euo pipefail
cd "$(dirname "$0")"

echo "==> running test suite"
uv run python -m pytest tests -q

if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: uncommitted changes — commit before deploying." >&2
    exit 1
fi

echo "==> building dashboard SPA"
npm --prefix ui ci --silent
npm --prefix ui run build --silent

echo "==> syncing venv"
uv sync --frozen

echo "==> pushing"
git push origin master

echo "==> reloading units + restarting bob.service"
systemctl --user daemon-reload
systemctl --user restart bob.service
sleep 8

code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8420/dashboard)
if [[ "$code" != "307" && "$code" != "200" ]]; then
    echo "ERROR: healthcheck failed (HTTP $code) — check: journalctl --user -u bob.service --since '2 min ago'" >&2
    exit 1
fi
echo "==> deployed (HTTP $code)"
