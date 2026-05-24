# Tap Card Design

## Context

When the LLM doesn't use its send tool (e.g. `send_whatsapp_message` or `email_reply`) during a dispatch, a "tap" follow-up call reminds it to do so. These tap dispatches create LLM call log entries with category suffix `_tap` (e.g. `whatsapp_incoming_tap`, `email_incoming_tap`).

Currently, the dashboard renders these like any other call — showing the tap reminder text as `contact_name: You generated a response but haven't sent it...`, which looks like the human participant said it.

## Design

Follow the existing ReflectionCard pattern: detect `_tap` category suffix in the timeline and render a dedicated TapCard component.

### TapCard component

**File**: `ui_app/src/routes/sessions/$sessionKey/index.tsx`

A minimal card with:
- **Label**: "follow-up" in muted text (10px)
- **Content**: response preview prefixed with "cyborg:" — no contact name, no tap prompt
- **Style**: `bg-muted/5 border-l-2 border-muted/30` — very subtle, even more muted than summary/reflection cards
- **No expand/collapse** — keep it minimal since taps are short

### Timeline detection

In the timeline rendering logic (same file), add a check for `_tap` suffix before the reflection check:

```tsx
entry.data.call_category.endsWith("_tap") ? (
  <TapCard key={`c-${entry.data.id}`} call={entry.data} />
)
```

### No backend changes needed

The tap dispatch already sets `call_category="{original}_tap"`. The contact_id is passed through, but the TapCard simply doesn't display it.

## Files Modified

| File | Change |
|------|--------|
| `ui_app/src/routes/sessions/$sessionKey/index.tsx` | Add TapCard component + timeline detection |
