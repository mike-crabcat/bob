# Tailscale serve + funnel — live configuration

**Why this file exists:** every ad-hoc change to serve/funnel has broken
something (paths dropped, dashboard exposed, Twilio webhooks unreachable).
This is the canonical snapshot + rebuild commands + rules. **Re-read it
before touching `tailscale serve`, and update it after every change.**

Snapshot date: **2026-09-20** (tailscale 1.86.2). Re-capture the live
truth any time with:

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

## Rebuild from scratch

```bash
tailscale funnel 443 on
tailscale serve --bg --https=443 /             http://127.0.0.1:8420
tailscale serve --bg --https=443 /voice        http://127.0.0.1:8420/voice
tailscale serve --bg --https=443 /files        http://127.0.0.1:8420/files
tailscale serve --bg --https=443 /phone/twiml  http://127.0.0.1:8420/phone/twiml
tailscale serve --bg --https=443 /phone/media  http://127.0.0.1:8420/phone/media
tailscale serve --bg --https=443 /phone/status http://127.0.0.1:8420/phone/status
tailscale serve --bg --https=443 /perth        http://127.0.0.1:8460
tailscale serve --bg --https=443 /radio        http://127.0.0.1:8010/stream
tailscale serve --bg --https=443 /radiotranscript http://127.0.0.1:8011/radiotranscript
tailscale serve --bg --https=443 /figurine/bob-figurine-v2.stl         http://127.0.0.1:8777/bob-figurine-v2.stl
tailscale serve --bg --https=443 /figurine/bob-figurine-source.png      http://127.0.0.1:8777/bob-figurine-source.png
tailscale serve --bg --https=443 /figurine/bob-figurine-watertight.stl  http://127.0.0.1:8777/bob-figurine-watertight.stl

# tailnet-only dashboard on 8443 (serve, NOT funnel)
tailscale serve --bg --https=8443 / http://127.0.0.1:5173
```

(Syntax per tailscale 1.86 — re-check `tailscale serve --help` after
upgrades.)

## Pending / planned

- **aus-legal MCP (in build 2026-09-20):** the uplift brief includes a
  streamable-http transport for this funnel so Helen can attach it in
  Claude. **Do not expose it without an auth token on the endpoint** —
  an unauthenticated MCP on the public funnel gives the whole internet
  corpus query + full-doc fetch. Register with a bearer header (same
  pattern as `zai-web-search`) and document the path here when it lands.
