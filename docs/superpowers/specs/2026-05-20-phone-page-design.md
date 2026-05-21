# Phone Page Design

## Context

The cyborg dashboard has a 5-tab bottom nav (Home, Sessions, Contacts, Skills, Workspace) but no way to view or manage phone calls from the UI. Phone calls are a core feature — they have a full backend (Twilio integration, call recording, exchange transcripts) but no frontend presence. This adds a Phone page to view calls, play recordings, and initiate new calls.

## Approach

Add a "..." overflow tab to the bottom nav that opens a popover with Phone as the first option. Phone gets two routes: a list page (`/phone`) and a detail page (`/phone/$callId`). Backend gets new dashboard API endpoints wrapping existing phone logic.

## Nav: Overflow Tab

- 6th tab in bottom nav: "..." icon, no label
- Tapping opens a popover above the tab bar with items (Phone icon + label)
- Tapping outside or selecting an item closes the popover
- "..." tab shows active state when any overflow route is active (e.g. `/phone`)
- Future overflow items can be added to the same popover

## Backend: Dashboard API Endpoints

New endpoints in `dashboard_api.py` under `/api/` prefix with auth:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/phone/calls` | GET | List recent phone calls with contact name |
| `/api/phone/calls/{call_id}` | GET | Call detail with all exchanges |
| `/api/phone/call` | POST | Initiate outbound call (body: `{to, agenda}`) |
| `/api/phone/recording/{call_id}` | GET | Serve WAV recording file |

These wrap existing phone router logic and DB queries.

## WebSocket: Live Call Events

New event types emitted on call state transitions:
- `phone.call.ringing` — new call initiated
- `phone.call.active` — call answered
- `phone.call.completed` — call ended
- `phone.call.exchange` — new exchange during active call

Root layout WS subscriber invalidates `["phone-calls"]` and `["phone-call", callId]` query keys.

## Phone List Page (`/phone`)

- Header with "Phone" title and "New call" button
- Active calls pinned at top with pulsing green dot, status badge, live exchange count
- Call list grouped by date (Today, Yesterday, older)
- Each row: direction arrow, phone number or contact name, agenda preview, exchange count, duration, relative time, status badge
- Click row → `/phone/$callId`

## New Call Form

- Triggered by "New call" button on phone list page
- "To" field: text input for phone number OR searchable contact dropdown (toggle between modes)
- "Agenda" textarea
- "Call" button: POST to `/api/phone/call`, navigate to call detail on success

## Call Detail Page (`/phone/$callId`)

- Back link "← phone"
- Call header: phone number/contact, direction, status, duration, time
- Agenda display
- Session link to `/sessions/$sessionKey` where `sessionKey = bobvoice:chat:phone:{call_id}`
- Audio player for WAV recording
- Exchange list: user/assistant transcripts with latency breakdown per exchange

## Files to Modify

- `packages/cyborg-server/cyborg_server/ui_app/src/routes/__root.tsx` — overflow tab + popover
- `packages/cyborg-server/cyborg_server/routers/dashboard_api.py` — phone API endpoints
- `packages/cyborg-server/cyborg_server/routers/phone.py` — emit WS events on state changes

## Files to Create

- `packages/cyborg-server/cyborg_server/ui_app/src/routes/phone/index.tsx` — call list page
- `packages/cyborg-server/cyborg_server/ui_app/src/routes/phone/$callId/index.tsx` — call detail page

## Verification

1. Run dev server (`cd packages/cyborg-server/cyborg_server/ui_app && npm run dev`)
2. Verify overflow tab appears in nav, popover opens with Phone option
3. Navigate to `/phone` — should show call list (empty or with existing calls)
4. Click "New call" — form appears with contact picker and phone number input
5. Navigate to `/phone/$callId` for an existing call — should show detail with transcript, session link, audio player
6. Verify WebSocket events update active call status in real-time
