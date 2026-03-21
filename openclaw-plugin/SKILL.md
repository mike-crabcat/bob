---
name: cyborg-context
description: Inject Cyborg context (projects, tasks, events) into Bob's context window. Use when Bob needs to know his active work items, project status, or upcoming events. Triggers on startup, context refresh requests, or when Bob asks about his tasks/projects.
---

# Cyborg Context Plugin

Fetches context from Cyborg data service and injects it into Bob's context window.

## Overview

Cyborg runs locally (default port 8420) and provides:
- Active projects
- Active tasks
- Upcoming events (7 days)

## Service URL from Webhooks

When Cyborg sends webhook notifications or task assignments via OpenClaw, the payload includes the `cyborg_service_url` field. Extract this URL and use it for all callbacks to the Cyborg API.

### Webhook Payload Example

```json
{
  "event": "task.completed",
  "timestamp": "2025-01-21T10:30:00Z",
  "title": "Task completed: Review PR",
  "message": "The pull request has been reviewed",
  "task_id": "abc-123",
  "cyborg_service_url": "http://localhost:8420",
  "metadata": {...}
}
```

### Extracting the Service URL

```python
def get_cyborg_url_from_notification(notification: dict) -> str:
    """Extract the Cyborg service URL from a webhook notification."""
    return notification.get("cyborg_service_url", "http://127.0.0.1:8420")

# Use for callbacks
cyborg_base = get_cyborg_url_from_notification(notification)
```

## Endpoints

- `GET /openclaw/context.txt` — Human-readable format
- `GET /openclaw/context.json` — JSON format
- `GET /api/v1/context/summary` — Full context summary
- `POST /api/v1/tasks/{task_id}/complete` — Complete a task

## Usage

### On Startup

Fetch and inject context at the start of each session:

```python
import requests

def get_cyborg_context(cyborg_url: str = "http://127.0.0.1:8420") -> str:
    try:
        resp = requests.get(f"{cyborg_url}/openclaw/context.txt", timeout=5)
        return resp.text
    except Exception:
        return "# Context unavailable\nCyborg service not running."

# Inject into system prompt
context = get_cyborg_context()
```

### Completing Tasks from Webhooks

When receiving a task assignment webhook, use the provided service URL:

```python
def complete_cyborg_task(notification: dict, result: str) -> bool:
    """Complete a Cyborg task using the URL from the notification."""
    cyborg_base = notification.get("cyborg_service_url", "http://127.0.0.1:8420")
    task_id = notification.get("metadata", {}).get("task_id")

    if not task_id:
        return False

    try:
        resp = requests.post(
            f"{cyborg_base}/api/v1/tasks/{task_id}/complete",
            json={"result_summary": result},
            timeout=10
        )
        return resp.status_code == 200
    except Exception:
        return False
```

### Manual Refresh

When Bob asks "what are my tasks?" or "show my projects":

```bash
curl -s http://127.0.0.1:8420/openclaw/context.txt
```

### JSON Access

For programmatic use:

```bash
curl -s http://127.0.0.1:8420/openclaw/context.json | python3 -m json.tool
```

## Context Format

The text format includes:

```
# Bob's Active Context
Generated: 2026-03-10 13:31 UTC

## Active Projects
- **Project Name**: Brief description
...

## Active Tasks
- 🔴 **Critical Task** (requested by: Mike)
- 🟠 **High Priority Task**
...

## Upcoming Events (7 days)
- 2026-03-15 14:00: **Meeting Title** @ Venue
...

## Summary
- Active projects: N
- Active tasks: N
- Upcoming events: N
```

## Priority Emoji

| Emoji | Priority |
|-------|----------|
| 🔴 | critical |
| 🟠 | high |
| 🟡 | medium |
| 🟢 | low |
| ⚪ | unknown |

## Development Instances

When running Cyborg on a custom port or host, set the `CYBORG_PUBLIC_URL` environment variable:

```bash
# Custom port
export CYBORG_PORT=8421
cyborg serve

# Or explicit public URL (for remote/dev instances)
export CYBORG_PUBLIC_URL="http://192.168.1.100:8420"
cyborg serve
```

The `cyborg_service_url` in webhook payloads will reflect the configured public URL.

## Service Status

Check if Cyborg is running:

```bash
curl -s http://127.0.0.1:8420/health
```

Start Cyborg if needed:

```bash
cd ~/.openclaw/workspace/projects/cyborg
export CYBORG_PORT=8420
export CYBORG_PUBLIC_URL="http://127.0.0.1:8420"
python3 -m uvicorn cyborg.main:app --host 127.0.0.1 --port 8420
```

## Files

- `../cyborg/routers/openclaw.py` — API endpoint implementation
- `../cyborg/routers/context.py` — Context endpoints
- `../cyborg/services/openclaw_hook_service.py` — Gateway integration with URL injection
- `../cyborg/openclaw-plugin/SKILL.md` — This file

## Integration Notes

- Cyborg must be running for context to be available
- Context is generated on-demand (not cached)
- Falls back gracefully if service unavailable
- Safe to call frequently (lightweight queries)
- **Always extract `cyborg_service_url` from webhook payloads for callbacks**
