# Cyborg Context Plugin for OpenClaw

Injects active projects, tasks, and events from the Cyborg service directly into OpenClaw conversations using the native Context Engine API.

Context is injected before the model processes each request, with automatic caching to reduce load on the Cyborg service.

## Installation

Copy the plugin to the OpenClaw extensions directory:

```bash
cp -r openclaw-plugin ~/.openclaw/extensions/cyborg-context
systemctl --user restart openclaw-gateway.service
```

For development, symlink instead:

```bash
ln -s "$(pwd)/openclaw-plugin" ~/.openclaw/extensions/cyborg-context
```

## Configuration

In `~/.config/openclaw/openclaw.json5`:

```json5
{
  plugins: {
    cyborgContext: {
      enabled: true,
      cyborgUrl: "http://127.0.0.1:8420",
      includeProjects: true,
      includeTasks: true,
      includeEvents: true,
      cacheTtlSeconds: 300,
      maxTokens: 2000
    }
  }
}
```

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `enabled` | boolean | `true` | Enable/disable context injection |
| `cyborgUrl` | string | `http://127.0.0.1:8420` | Cyborg service URL |
| `includeProjects` | boolean | `true` | Include active projects |
| `includeTasks` | boolean | `true` | Include active tasks |
| `includeEvents` | boolean | `true` | Include upcoming events |
| `cacheTtlSeconds` | integer | `300` | Cache TTL in seconds |
| `maxTokens` | integer | `2000` | Max tokens for Cyborg context |

## Prerequisites

1. Cyborg service running (`uv run cyborg status`)
2. Context API accessible (`curl http://127.0.0.1:8420/api/v1/context/summary`)
