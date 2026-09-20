# Tailscale serve + funnel — live configuration

**Why this file exists:** every ad-hoc change to serve/funnel has broken
something (paths dropped, dashboard exposed, Twilio webhooks unreachable).
This is the canonical snapshot + rebuild commands + rules. **Re-read it
before touching `tailscale serve`, and update it after every change.**

Snapshot date: **2026-09-20** (tailscale 1.86.2 — **the serve/funnel CLI
syntax changed in 1.86**; the rebuild commands below are the new
`--set-path` form, verified live). Re-capture the live truth any time with:

```bash
tailscale serve status
```

## Node facts

| | |
|---|---|
| Host | `mike-workstation` (100.73.201.65) |
| Tailnet | `mike@` → `tail94e30e.ts.net` |
| Public base URL | `https://mike-workstation.tail94e30e.ts.net` |
| Daemon | system `tailscaled.service` — serve config lives in node state, survives reboots |

## Vhost: 443 — **FUNNEL ON (public internet)**

Everything under this host is reachable by anyone on the internet. Each
line is load-bearing for a live integration:

| Path | Upstream | Served by | What it is |
|---|---|---|---|
| `/` | `http://127.0.0.1:8420` | `bob.service` | Bob FastAPI root |
| `/voice` | `http://127.0.0.1:8420/voice` | `bob.service` | Browser realtime voice sessions (`/voice/realtime`) |
| `/files` | `http://127.0.0.1:8420/files` | `bob.service` | File serving |
| `/phone/twiml` | `http://127.0.0.1:8420/phone/twiml` | `bob.service` | **Twilio voice webhooks — must stay public** (Twilio POSTs here) |
| `/phone/media` | `http://127.0.0.1:8420/phone/media` | `bob.service` | **Twilio media-stream bridge — must stay public** |
| `/phone/status` | `http://127.0.0.1:8420/phone/status` | `bob.service` | Twilio call status callbacks |
| `/perth` | `http://127.0.0.1:8460` | `perth` (python, port 8460) | PERTH 2032 game |
| `/radio` | `http://127.0.0.1:8010/stream` | `bg-bob-fm.service` (`skills/radio/station.py`) | Bob's Pirate Radio stream mount |
| `/aus-legal` | `http://127.0.0.1:8017/mcp` | `bg-aus-legal.service` (`skills/aus-legal/scripts/aus_legal_mcp.py`) | Australian legal corpus MCP — bearer-gated (token = `BOB_AUS_LEGAL_TOKEN` / `skills/aus-legal/api_key`); ONLY `/mcp` is public, the plain-HTTP `/doc` `/search` `/cite` routes stay loopback-only |
| `/radiotranscript` | `http://127.0.0.1:8011/radiotranscript` | radio transcript host (python, 8011) | Live radio transcript feed |
| `/figurine/bob-figurine-v2.stl` | `http://127.0.0.1:8777/…` | `bg-stl-server.service` (`http.server` on scratch/) | Figurine print downloads |
| `/figurine/bob-figurine-source.png` | `http://127.0.0.1:8777/…` | 〃 | 〃 |
| `/figurine/bob-figurine-watertight.stl` | `http://127.0.0.1:8777/…` | 〃 | 〃 |

## Vhost: 8443 — **tailnet only (NOT funnelled)**

| Path | Upstream | Served by | What it is |
|---|---|---|---|
| `/` | `http://127.0.0.1:5173` | `bob-ui.service` (vite dev server) | Dashboard SPA — **must stay tailnet-only** |

## Rules for changes

1. **Snapshot first:** `tailscale serve status | tee /tmp/serve-before.txt`.
2. **One path at a time**, then verify from OUTSIDE the tailnet (phone on
   mobile data, or `curl` from a non-tailnet host) — a path that works from
   inside the tailnet can still be funnel-broken.
3. **Never toggle funnel off on 443** to "fix" anything — the three
   `/phone/*` paths are Twilio's inbound webhooks; funnel-off silently kills
   all phone calls.
4. **Never move the dashboard (5173) onto the 443 funnel vhost.**
5. Upstream must listen on exactly the path serve forwards
   (`/radio` → `:8010/stream`, `/voice` → `:8420/voice`) — check the local
   port is listening before blaming serve: `ss -tlnp | grep <port>`.
6. Serve config survives reboots (node state) — a `systemctl restart
   tailscaled` is not needed and does not reset anything.
7. **Update this file after every change** (path table + snapshot date).
8. **1.86 CLI: any `tailscale serve` mutation on the 443 vhost STRIPS the
   funnel** ("Removing Funnel …:443" in the output — it does not ask).
   Confirmed live 2026-09-20 adding `/aus-legal` (~2 min funnel-off window).
   After EVERY serve change on 443, re-apply the funnel root mount:
   `tailscale funnel --bg --https=443 --set-path=/ --yes http://127.0.0.1:8420`
   and confirm `tailscale funnel status` shows **Funnel on** before walking
   away. Old-syntax commands (`tailscale funnel 443 on`,
   `tailscale serve --bg --https=443 /path target`) now error out.

## Rebuild from scratch

New (1.86) syntax, in this order — mounts first, funnel LAST (rule 8):

```bash
tailscale serve --bg --https=443 --set-path=/             --yes http://127.0.0.1:8420
tailscale serve --bg --https=443 --set-path=/voice        --yes http://127.0.0.1:8420/voice
tailscale serve --bg --https=443 --set-path=/files        --yes http://127.0.0.1:8420/files
tailscale serve --bg --https=443 --set-path=/phone/twiml  --yes http://127.0.0.1:8420/phone/twiml
tailscale serve --bg --https=443 --set-path=/phone/media  --yes http://127.0.0.1:8420/phone/media
tailscale serve --bg --https=443 --set-path=/phone/status --yes http://127.0.0.1:8420/phone/status
tailscale serve --bg --https=443 --set-path=/perth        --yes http://127.0.0.1:8460
tailscale serve --bg --https=443 --set-path=/radio        --yes http://127.0.0.1:8010/stream
tailscale serve --bg --https=443 --set-path=/radiotranscript --yes http://127.0.0.1:8011/radiotranscript
tailscale serve --bg --https=443 --set-path=/figurine/bob-figurine-v2.stl         --yes http://127.0.0.1:8777/bob-figurine-v2.stl
tailscale serve --bg --https=443 --set-path=/figurine/bob-figurine-source.png      --yes http://127.0.0.1:8777/bob-figurine-source.png
tailscale serve --bg --https=443 --set-path=/figurine/bob-figurine-watertight.stl  --yes http://127.0.0.1:8777/bob-figurine-watertight.stl
tailscale serve --bg --https=443 --set-path=/aus-legal    --yes http://127.0.0.1:8017/mcp

# turn the whole 443 vhost public (the only funnel step — mounts above are tailnet-only until this runs)
tailscale funnel --bg --https=443 --set-path=/ --yes http://127.0.0.1:8420

# tailnet-only dashboard on 8443 (serve, NOT funnel)
tailscale serve --bg --https=8443 --set-path=/ --yes http://127.0.0.1:5173
```

(Re-check `tailscale serve --help` after upgrades — the syntax already
changed once, see rule 8.)

## Pending / planned

- **aus-legal funnel — OUTSIDE verification still owed (2026-09-20):** the
  `/aus-legal` path is live and bearer-gated (verified 401-no-token /
  200-with-token from the box, via the tailnet-hairpin URL). Still needs one
  check from OUTSIDE the tailnet (phone on mobile data, rule 2), and Helen's
  first real attach: `claude mcp add --transport http --header
  "Authorization: Bearer <token>" aus-legal
  https://mike-workstation.tail94e30e.ts.net/aus-legal`. Token distributes
  out-of-band; it's `skills/aus-legal/api_key` on this box.
- Reboot note: the serve config survives reboots, but `bg-aus-legal.service`
  does not (transient bg unit) — after a reboot the public path 502s until
  Bob starts the server again (skill.md documents this).
