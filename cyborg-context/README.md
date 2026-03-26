# Cyborg Context Plugin for OpenClaw

Inject Bob's active projects, tasks, and events from Cyborg service directly into OpenClaw conversations using the native Context Engine API.

## Why a Plugin Instead of a Skill?

This plugin uses OpenClaw's **Context Engine API**, which is the intended mechanism for injecting structured context into conversations:

| Aspect | Skill (`SKILL.md`) | Plugin (`ContextEngine`) |
|---------|----------------------|---------------------------|
| **Mechanism** | LLM reads documentation | Native runtime integration |
| **Reliability** | Depends on LLM following docs | Guaranteed - called by runtime |
| **Context Injection** | External HTTP call | Direct injection via `assemble()` |
| **Startup** | Manual or keyword-triggered | Automatic on every session |
| **Compaction** | N/A | Built-in cache management |
| **Lifecycle** | None | Full lifecycle hooks |

The plugin is significantly more reliable because:
- Context is injected **before** the model processes the request
- No external tool calls needed
- Automatic caching reduces Cyborg service load
- Token-aware context assembly prevents overflows

## Installation

### Option 1: Install to OpenClaw Extensions Directory (Recommended)

```bash
# Navigate to the plugin directory
cd ~/.openclaw/workspace/projects/cyborg/openclaw-plugin

# Copy to OpenClaw extensions
cp -r ~/.openclaw/workspace/projects/cyborg/openclaw-plugin ~/.openclaw/extensions/cyborg-context

# Verify installation
ls ~/.openclaw/extensions/cyborg-context/
# Should show: index.ts, openclaw.plugin.json, package.json, README.md

# Restart OpenClaw gateway
systemctl --user restart openclaw-gateway.service
```

### Option 2: Development Installation (Symlink)

For development, symlink into the extensions directory:

```bash
ln -s ~/.openclaw/workspace/projects/cyborg/openclaw-plugin \
      ~/.openclaw/extensions/cyborg-context
```

## Configuration

The plugin can be configured in OpenClaw's config (usually `~/.config/openclaw/openclaw.json5`):

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

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `enabled` | boolean | `true` | Enable/disable context injection |
| `cyborgUrl` | string | `http://127.0.0.1:8420` | Cyborg service URL |
| `includeProjects` | boolean | `true` | Include active projects |
| `includeTasks` | boolean | `true` | Include active tasks |
| `includeEvents` | boolean | `true` | Include upcoming events |
| `cacheTtlSeconds` | integer | `300` | Cache TTL in seconds (5 min) |
| `maxTokens` | integer | `2000` | Max tokens for Cyborg context |

## Prerequisites

1. **Cyborg service running**:
   ```bash
   uv run cyborg status
   # or start if needed
   uv run cyborg start
   ```

2. **Cyborg context API accessible**:
   ```bash
   curl http://127.0.0.1:8420/api/v1/context/summary
   ```

3. **OpenClaw 2026.3.23+ installed** (with Context Engine support)

## Usage

Once installed and enabled, Cyborg context is automatically injected into every OpenClaw conversation. The context includes:

- **Active Projects** with aims and descriptions
- **Active Tasks** with priority emojis and requested-by info
- **Upcoming Events** (next 7 days) with times and venues
- **Summary counts** for quick reference

### Context Format

The injected context follows this format:

```markdown
# Bob's Active Context
Generated: 2026-03-24 19:30 UTC

## Active Projects
- **Project Name**: Project aim statement

## Active Tasks
- 🔴 **Critical Task** (Project Name)
  Requested by: Mike
- 🟠 **High Priority Task**
- 🟡 **Medium Priority Task**
- 🟢 **Low Priority Task**

## Upcoming Events (7 days)
- 2026-03-28 14:00: **Team Meeting** @ Conf Room A

## Summary
- Active projects: 2
- Active tasks: 5
- Upcoming events: 3
```

## Troubleshooting

### Plugin Not Loading

```bash
# Check OpenClaw logs for plugin registration
journalctl --user -u openclaw-gateway.service -n 100

# Verify plugin files exist
ls -la ~/.openclaw/extensions/cyborg-context/

# Check OpenClaw version supports plugins (2026.3.23+)
openclaw --version
```

### Context Not Appearing

```bash
# Verify Cyborg is accessible
curl http://127.0.0.1:8420/api/v1/context/summary

# Check plugin is enabled in config
cat ~/.config/openclaw/openclaw.json5 | grep cyborgContext

# Force refresh cache by restarting
systemctl --user restart openclaw-gateway.service
```

### Context Too Large

If the Cyborg context exceeds the `maxTokens` setting, it will be skipped for that turn:

```json5
{
  plugins: {
    cyborgContext: {
      maxTokens: 4000  // Increase for more context
    }
  }
}
```

## Development

To build/test changes:

```bash
# Navigate to plugin directory
cd ~/.openclaw/workspace/projects/cyborg/openclaw-plugin

# The plugin is loaded dynamically by OpenClaw
# No build step required - TypeScript is handled at runtime
```

## License

MIT

## Author

Mike
