# Cyborg Webhooks Integration

This document describes how to integrate Cyborg webhooks with OpenClaw for event-driven project execution.

## Overview

Cyborg can send HTTP webhooks to OpenClaw when significant events occur:
- `task.completed` - A task is completed
- `task.failed` - A task fails
- `project.blocked` - A project is blocked waiting for input
- `project.ready_for_review` - A project is ready for human review

## OpenClaw Webhook Endpoint

### Endpoint

```
POST /webhook/cyborg
```

### Headers

| Header | Description |
|--------|-------------|
| `Content-Type` | `application/json` |
| `X-Webhook-Signature` | HMAC-SHA256 signature: `sha256=<hex>` |
| `X-Webhook-Event` | Event type (e.g., `task.completed`) |
| `X-Webhook-ID` | Unique delivery ID |
| `X-Webhook-Attempt` | Delivery attempt number (1-indexed) |

### Payload Format

```json
{
  "event": "task.completed",
  "timestamp": "2026-03-12T12:30:00Z",
  "project_id": "uuid",
  "task_id": "uuid",
  "task_title": "Task name",
  "result_summary": "What was accomplished",
  "session_key": "sess_abc123",
  "metadata": {}
}
```

### Signature Verification

Verify the HMAC-SHA256 signature to ensure the webhook is authentic:

```python
import hmac
import hashlib

def verify_signature(payload: str, signature: str, secret: str) -> bool:
    """Verify webhook signature."""
    # Signature format: sha256=<hex>
    expected = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    given = signature.replace("sha256=", "") if signature.startswith("sha256=") else signature
    return hmac.compare_digest(expected, given)
```

## Event Types

### task.completed

Triggered when a task is marked as completed.

```json
{
  "event": "task.completed",
  "timestamp": "2026-03-12T12:30:00Z",
  "project_id": "550e8400-e29b-41d4-a716-446655440000",
  "task_id": "550e8400-e29b-41d4-a716-446655440001",
  "task_title": "Implement webhook service",
  "result_summary": "Created webhook_service.py with HMAC signatures and retry logic",
  "session_key": "sess_abc123",
  "metadata": {
    "channel": "whatsapp",
    "chat_id": "120363426096069246@g.us"
  }
}
```

**OpenClaw Action:**
- If `session_key` is present, announce completion in the originating session
- Check if linked projects should progress to next step
- Spawn subagent if project auto-execution is enabled

### task.failed

Triggered when a task fails.

```json
{
  "event": "task.failed",
  "timestamp": "2026-03-12T12:30:00Z",
  "project_id": "550e8400-e29b-41d4-a716-446655440000",
  "task_id": "550e8400-e29b-41d4-a716-446655440001",
  "task_title": "Deploy to production",
  "result_summary": "Connection timeout after 30s",
  "session_key": "sess_abc123",
  "metadata": {}
}
```

**OpenClaw Action:**
- Notify user of failure
- Check retry configuration
- Offer to retry or escalate

### project.blocked

Triggered when a project is blocked waiting for user input.

```json
{
  "event": "project.blocked",
  "timestamp": "2026-03-12T12:30:00Z",
  "project_id": "550e8400-e29b-41d4-a716-446655440000",
  "task_title": "Q1 Data Migration",
  "result_summary": "Waiting for API key from David",
  "session_key": "sess_abc123",
  "metadata": {
    "resume_instructions": "When unblocked: 1) Add key to .env 2) Test connection"
  }
}
```

**OpenClaw Action:**
- Announce blocker to user in the originating channel
- Include resume instructions
- Set up reminder to check back

### project.ready_for_review

Triggered when a project is ready for human review.

```json
{
  "event": "project.ready_for_review",
  "timestamp": "2026-03-12T12:30:00Z",
  "project_id": "550e8400-e29b-41d4-a716-446655440000",
  "task_title": "Q1 Data Migration",
  "result_summary": "All success criteria met. 150 records migrated.",
  "session_key": "sess_abc123",
  "metadata": {
    "review_notes": "Please review the migrated data before closing"
  }
}
```

**OpenClaw Action:**
- Notify user that project is ready for review
- Summarize accomplishments
- Offer to close project or continue

## Configuration

### Environment Variables

Configure webhooks via environment variables:

```bash
# Webhook endpoint URL
export CYBORG_WEBHOOK_OPENCLAW_URL=http://127.0.0.1:8080/webhook/cyborg

# Webhook secret for HMAC signature
export CYBORG_WEBHOOK_OPENCLAW_SECRET=your-secret-key

# Comma-separated list of events to subscribe to
export CYBORG_WEBHOOK_OPENCLAW_EVENTS=task.completed,task.failed,project.blocked,project.ready_for_review

# Retry count (default: 3)
export CYBORG_WEBHOOK_OPENCLAW_RETRY_COUNT=3
```

### Programmatic Configuration

Create webhook configs via the API:

```bash
curl -X POST http://127.0.0.1:8420/api/v1/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "openclaw",
    "url": "http://127.0.0.1:8080/webhook/cyborg",
    "secret": "your-secret-key",
    "events": ["task.completed", "task.failed", "project.blocked", "project.ready_for_review"],
    "retry_count": 3
  }'
```

## Retry Behavior

Cyborg implements exponential backoff for failed webhook deliveries:

| Attempt | Delay |
|---------|-------|
| 1 | Immediate |
| 2 | 10 seconds |
| 3 | 20 seconds |
| 4 | 40 seconds |

After exhausting retries, the delivery is marked as failed and can be retried manually via the API.

## Delivery Tracking

All webhook deliveries are tracked in the database:

```bash
# List recent deliveries
curl http://127.0.0.1:8420/api/v1/webhooks/deliveries

# Get delivery details
curl http://127.0.0.1:8420/api/v1/webhooks/deliveries/<delivery-id>

# Retry a failed delivery
curl -X POST http://127.0.0.1:8420/api/v1/webhooks/deliveries/<delivery-id>/retry
```

## Security Considerations

1. **Always verify signatures** - Use the HMAC-SHA256 signature to verify webhooks
2. **Use HTTPS in production** - Configure webhook URLs with HTTPS
3. **Keep secrets secure** - Store webhook secrets in environment variables or secrets manager
4. **Idempotency** - Webhook deliveries may be retried; handle duplicate events gracefully
5. **Timeout handling** - OpenClaw should respond within 30 seconds or Cyborg will retry

## Example: OpenClaw Handler

```python
from fastapi import FastAPI, Request, HTTPException
import hmac
import hashlib

app = FastAPI()
WEBHOOK_SECRET = "your-secret-key"

@app.post("/webhook/cyborg")
async def handle_cyborg_webhook(request: Request):
    payload = await request.body()
    signature = request.headers.get("X-Webhook-Signature", "")
    event = request.headers.get("X-Webhook-Event")
    
    # Verify signature
    if not verify_signature(payload.decode(), signature, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid signature")
    
    data = await request.json()
    
    # Handle event
    if event == "task.completed":
        await handle_task_completed(data)
    elif event == "task.failed":
        await handle_task_failed(data)
    elif event == "project.blocked":
        await handle_project_blocked(data)
    elif event == "project.ready_for_review":
        await handle_project_ready(data)
    
    return {"status": "ok"}

def verify_signature(payload: str, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    given = signature.replace("sha256=", "")
    return hmac.compare_digest(expected, given)

async def handle_task_completed(data: dict):
    session_key = data.get("session_key")
    task_title = data.get("task_title")
    result = data.get("result_summary")
    
    # Spawn subagent or announce in channel
    print(f"Task completed: {task_title}")
    print(f"Result: {result}")
```
