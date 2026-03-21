# Task: OpenClaw Plugin Integration for Cyborg

**Task ID:** (pending - Cyborg CLI has enum bug)  
**Requested by:** Mike  
**Priority:** High  
**Status:** Active  
**Project:** Cyborg

---

## Objective

Create a proper OpenClaw plugin that automatically injects Cyborg context (projects, tasks, events) into Bob's context window on every session start.

---

## Research Findings: OpenClaw Plugin System

### Plugin Architecture

OpenClaw plugins are **TypeScript modules** that extend the Gateway with:
- Agent tools (JSON-schema functions)
- Gateway RPC methods
- HTTP routes
- CLI commands
- Background services
- Context engines
- Hooks (lifecycle events)

### Key Integration Points for Cyborg

| Feature | API | Use Case |
|---------|-----|----------|
| **Agent Tools** | `api.registerTool()` | Allow Bob to query Cyborg on-demand |
| **Hooks** | `api.on("before_prompt_build")` | Auto-inject context at session start |
| **HTTP Routes** | `api.registerHttpRoute()` | Expose Cyborg data to external systems |
| **CLI Commands** | `api.registerCli()` | Add `openclaw cyborg status` command |

### Recommended Approach: Context Injection Hook

The best integration is via **`before_prompt_build` hook**:

```typescript
api.on("before_prompt_build", (event, ctx) => {
  // Fetch context from Cyborg
  const cyborgContext = await fetchCyborgContext();
  
  return {
    prependSystemContext: cyborgContext,
  };
});
```

This automatically prepends Cyborg context to Bob's system prompt on every session.

---

## Proposed Implementation Plan

### Phase 1: Basic Plugin Structure (2-3 hours)

1. **Create plugin directory**: `~/.openclaw/extensions/cyborg-context/`
2. **Create manifest**: `openclaw.plugin.json`
3. **Create entry point**: `index.ts`
4. **Register hook**: `before_prompt_build` to fetch from Cyborg API

### Phase 2: Context Fetching (2-3 hours)

1. **HTTP client**: Fetch from `http://127.0.0.1:8420/openclaw/context.txt`
2. **Error handling**: Graceful fallback if Cyborg is down
3. **Caching**: Optional caching to avoid repeated requests

### Phase 3: Configuration (1-2 hours)

1. **Config schema**: Allow users to configure:
   - Cyborg URL (default: localhost:8420)
   - Enable/disable auto-inject
   - Context format (full/compact/none)
2. **UI hints**: Labels and help text

### Phase 4: Optional Tools (3-4 hours, future)

1. **`cyborg_query` tool**: Let Bob query Cyborg on-demand
2. **`cyborg_add_task` tool**: Add tasks directly from conversations
3. **`cyborg_update_project` tool**: Update project status

---

## File Structure

```
~/.openclaw/extensions/cyborg-context/
├── openclaw.plugin.json    # Plugin manifest
├── index.ts                # Main entry point
├── package.json            # NPM dependencies
├── tsconfig.json           # TypeScript config
└── src/
    ├── client.ts           # Cyborg HTTP client
    ├── context.ts          # Context formatting
    └── config.ts           # Config types/validation
```

---

## Manifest (openclaw.plugin.json)

```json
{
  "id": "cyborg-context",
  "name": "Cyborg Context Injector",
  "description": "Automatically injects Cyborg project/task context into Bob's context window",
  "version": "1.0.0",
  "configSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "cyborgUrl": {
        "type": "string",
        "default": "http://127.0.0.1:8420"
      },
      "enabled": {
        "type": "boolean",
        "default": true
      },
      "format": {
        "type": "string",
        "enum": ["full", "compact", "none"],
        "default": "full"
      }
    }
  },
  "uiHints": {
    "cyborgUrl": { "label": "Cyborg API URL" },
    "enabled": { "label": "Enable context injection" },
    "format": { "label": "Context format" }
  }
}
```

---

## Implementation Sketch (index.ts)

```typescript
import type { PluginAPI } from "openclaw/plugin-sdk/core";

export default function register(api: PluginAPI) {
  // Register context injection hook
  api.on("before_prompt_build", async () => {
    const config = api.config.plugins.entries["cyborg-context"]?.config;
    if (!config?.enabled) return {};

    try {
      const response = await fetch(`${config.cyborgUrl}/openclaw/context.txt`);
      const context = await response.text();
      
      return {
        prependSystemContext: `\n${context}\n`,
      };
    } catch (err) {
      api.logger.warn("Failed to fetch Cyborg context:", err);
      return {};
    }
  });
}
```

---

## Open Questions

1. **TypeScript setup**: Do we need a build step or use jiti (runtime TS)?
2. **Error handling**: Should failed context fetch block session start?
3. **Caching**: Should we cache context for N minutes to reduce requests?
4. **User control**: Should users be able to disable per-session via command?

---

## Next Steps

1. **Decision**: Approve plan and prioritize phases
2. **Setup**: Create plugin directory and basic structure
3. **Implement**: Build and test Phase 1-2
4. **Configure**: Add to OpenClaw config and test integration

---

## References

- OpenClaw Plugin Docs: `/usr/lib/node_modules/openclaw/docs/tools/plugin.md`
- Plugin Manifest: `/usr/lib/node_modules/openclaw/docs/plugins/manifest.md`
- Agent Tools: `/usr/lib/node_modules/openclaw/docs/plugins/agent-tools.md`
- Cyborg API: `http://127.0.0.1:8420/openclaw/context.txt`
