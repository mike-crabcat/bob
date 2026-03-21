---
name: cyborg-context
description: Inject Cyborg context (projects, tasks, events) into Bob's context window. Use when Bob needs to know his active work items, project status, or upcoming events. Triggers on startup, context refresh requests, or when Bob asks about his tasks/projects.
---

# Cyborg Context Plugin

Fetches context from Cyborg data service and injects it into Bob's context window.

## Overview

Cyborg runs locally on port 8420 and provides:
- Active projects
- Active tasks  
- Upcoming events (7 days)

## Endpoints

- `GET http://127.0.0.1:8420/openclaw/context.txt` — Human-readable format
- `GET http://127.0.0.1:8420/openclaw/context.json` — JSON format
- `GET http://127.0.0.1:8420/api/v1/context/summary` — Full context summary

## Usage

### On Startup (Recommended)

Fetch and inject context at the start of each session:

```python
import requests

def get_cyborg_context() -> str:
    try:
        resp = requests.get("http://127.0.0.1:8420/openclaw/context.txt", timeout=5)
        return resp.text
    except Exception:
        return "# Context unavailable\nCyborg service not running."

# Inject into system prompt
context = get_cyborg_context()
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

## Service Status

Check if Cyborg is running:

```bash
curl -s http://127.0.0.1:8420/health
```

Start Cyborg if needed:

```bash
cd ~/.openclaw/workspace/projects/cyborg
python3 -c "
import sys
sys.path.insert(0, '.')
from cyborg.main import create_app
from cyborg.config import Settings
import uvicorn

settings = Settings.from_env()
settings.ensure_directories()
app = create_app(settings)
uvicorn.run(app, host='127.0.0.1', port=8420)
" &
```

## Files

- `../cyborg/routers/openclaw.py` — API endpoint implementation
- `../cyborg/openclaw-plugin/SKILL.md` — This file

## Integration Notes

- Cyborg must be running for context to be available
- Context is generated on-demand (not cached)
- Falls back gracefully if service unavailable
- Safe to call frequently (lightweight queries)
