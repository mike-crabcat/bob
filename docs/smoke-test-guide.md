# Dashboard Smoke Test Guide

## Prerequisites

1. **Chrome running with remote debugging** on port 9222:
   ```
   google-chrome --remote-debugging-port=9222
   ```
2. **Chrome DevTools MCP** configured in `~/.mcp.json` (already set up):
   ```json
   { "mcpServers": { "chrome-devtools": { "command": "npx", "args": ["chrome-devtools-mcp@latest", "--browserUrl=http://127.0.0.1:9222"] } } }
   ```
3. **Backend server running** (`bob serve` on port 8420)
4. **Frontend dev server running** (`npm run dev` in `bob_server/ui_app/` on port 5173)

If testing production build instead of Vite dev, run `npm run build` first and use port 8420 directly.

## Pages to Check

| Page | URL (dev) | URL (prod) |
|------|-----------|------------|
| Home | `http://localhost:5173/dashboard/` | `http://localhost:8420/dashboard/` |
| Sessions list | `http://localhost:5173/dashboard/sessions` | `http://localhost:8420/dashboard/sessions` |
| Session detail | `http://localhost:5173/dashboard/sessions/<encoded-session-key>` | same pattern |

## Step-by-step

### 1. Open the Home page

```
navigate_page type=url url=http://localhost:5173/dashboard/
```

**Verify:**
- Header shows "bob" + green "live" indicator
- "ACTIVITY" section with recent events
- "SESSIONS" section with clickable session links (channel dots, call counts, relative timestamps)
- "LLM CALLS · 24H" chart renders with bar graph
- "SUMMARIES" section with recent session summaries
- "STATS" grid showing projects, active tasks, dispatches, sessions counts
- Bottom nav: "Home" (active/highlighted) and "Sessions" links

### 2. Check console for errors

```
list_console_messages types=["error", "warn"]
```

**Acceptable warnings (harmless):**
- `WebSocket connection ... failed: WebSocket is closed before the connection is established` — momentary race during page load/reload
- `Failed to load resource: 404 (Not Found)` on `favicon.ico` — no favicon configured

**Must NOT appear:**
- `No queryFn was passed` — TanStack Query error, means `useQuery` is missing `queryFn`
- `The width(-1) and height(-1) of chart should be greater than 0` — Recharts render-before-layout issue
- Any React error boundary messages or uncaught exceptions

### 3. Navigate to Sessions list

Click the "Sessions" nav link, or:
```
navigate_page type=url url=http://localhost:5173/dashboard/sessions
```

**Verify:**
- Channel filter chips at top (all, whatsapp, email, other, voice) — clicking filters the list
- Each row shows: colored channel dot, channel label, session key, call count, failed count, avg latency, relative time
- Rows are clickable links pointing to session detail

### 4. Navigate to Session detail

Click any session row, or navigate directly:
```
navigate_page type=url url=http://localhost:5173/dashboard/sessions/agent%3Amain%3Awhatsapp%3Adm%3A61456224867
```

**Verify:**
- "← sessions" back link works
- Session key displayed as heading
- Channel badge + stats (total calls, ok count, failed count if any)
- "PARTICIPANTS" section with display names and trust indicators
- "AGENDA" section (if session has one) showing system prompt/agenda text
- "SUMMARIES" section with summary text and topic tags
- "CALLS (N)" section listing LLM calls with category, model, status, latency, user message preview, response preview

### 5. Re-check console on each page

Repeat `list_console_messages types=["error", "warn"]` after each navigation. Only the harmless WS/favicon warnings should appear.

### 6. Test navigation round-trip

Home → Sessions → pick a session → back to Sessions → back to Home. All pages should render without errors and show live data (green "live" indicator stays on).

## Quick Commands Reference

| Action | Tool + params |
|--------|--------------|
| Go to page | `navigate_page type=url url=<url>` |
| Reload page | `navigate_page type=reload ignoreCache=true` |
| Check console | `list_console_messages types=["error","warn"]` |
| Get detailed error | `get_console_message msgid=<id>` |
| Check network requests | `list_network_requests` |
| Take screenshot | `take_screenshot` |
| Read page content | `take_snapshot` |
| Emulate mobile | `emulate viewport="390x844x2,mobile,touch"` |
| Click element | `click uid=<uid>` (get uid from snapshot) |
| Wait for content | `wait_for text=["some text"] timeout=5000` |
