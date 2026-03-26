/**
 * @cyborg/openclaw-plugin — Cyborg Context Plugin for OpenClaw
 *
 * Injects Bob's active projects, tasks, and events from Cyborg service
 * directly into the conversation context via OpenClaw's ContextEngine API.
 */

import type {
  AgentMessage,
  AssembleResult,
  BootstrapResult,
  CompactResult,
  ContextEngine,
  ContextEngineInfo,
  IngestResult,
} from "openclaw/plugin-sdk";

type CyborgContextConfig = {
  enabled: boolean;
  cyborgUrl: string;
  includeProjects: boolean;
  includeTasks: boolean;
  includeEvents: boolean;
  cacheTtlSeconds: number;
  maxTokens: number;
};

type CyborgContextResponse = {
  generated_at: string;
  projects: Array<{
    id: string;
    title: string;
    aim: string;
    description?: string;
  }>;
  tasks: Array<{
    id: string;
    title: string;
    priority: string;
    requested_by?: string;
    project_id?: string;
    project_title?: string;
  }>;
  events: Array<{
    id: string;
    title: string;
    start_time: string;
    venue?: string;
    calendar_name?: string;
  }>;
  counts: {
    active_projects: number;
    active_tasks: number;
    upcoming_events: number;
  };
};

type CyborgContextCache = {
  context: CyborgContextResponse | null;
  timestamp: number;
  text: string;
};

/**
 * Parse plugin config from OpenClaw's runtime config.
 */
function parseCyborgConfig(
  config: unknown,
): CyborgContextConfig {
  const defaults: CyborgContextConfig = {
    enabled: true,
    cyborgUrl: "http://127.0.0.1:8420",
    includeProjects: true,
    includeTasks: true,
    includeEvents: true,
    cacheTtlSeconds: 300,
    maxTokens: 2000,
  };

  if (!config || typeof config !== "object") {
    return defaults;
  }

  const cfg = config as Record<string, unknown>;
  return {
    enabled: typeof cfg.enabled === "boolean" ? cfg.enabled : defaults.enabled,
    cyborgUrl:
      typeof cfg.cyborgUrl === "string" ? cfg.cyborgUrl.trim() : defaults.cyborgUrl,
    includeProjects:
      typeof cfg.includeProjects === "boolean"
        ? cfg.includeProjects
        : defaults.includeProjects,
    includeTasks:
      typeof cfg.includeTasks === "boolean" ? cfg.includeTasks : defaults.includeTasks,
    includeEvents:
      typeof cfg.includeEvents === "boolean" ? cfg.includeEvents : defaults.includeEvents,
    cacheTtlSeconds:
      typeof cfg.cacheTtlSeconds === "number" && cfg.cacheTtlSeconds > 0
        ? cfg.cacheTtlSeconds
        : defaults.cacheTtlSeconds,
    maxTokens:
      typeof cfg.maxTokens === "number" && cfg.maxTokens >= 0
        ? cfg.maxTokens
        : defaults.maxTokens,
  };
}

/**
 * Estimate tokens for a text string (rough approximation).
 * Uses ~4 characters per token as a safe estimate.
 */
function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

/**
 * Format Cyborg context as a compact markdown string.
 */
function formatCyborgContext(data: CyborgContextResponse): string {
  const lines: string[] = [
    `# Bob's Active Context`,
    `Generated: ${data.generated_at}`,
    "",
  ];

  if (data.counts.active_projects > 0 && data.projects.length > 0) {
    lines.push("## Active Projects");
    for (const p of data.projects) {
      const aim = p.aim ? `: ${p.aim}` : "";
      lines.push(`- **${p.title}**${aim}`);
    }
    lines.push("");
  }

  if (data.counts.active_tasks > 0 && data.tasks.length > 0) {
    lines.push("## Active Tasks");
    const priorityEmoji: Record<string, string> = {
      critical: "🔴",
      high: "🟠",
      medium: "🟡",
      low: "🟢",
    };

    for (const t of data.tasks) {
      const emoji = priorityEmoji[t.priority] || "⚪";
      const projectRef = t.project_title ? ` (${t.project_title})` : "";
      lines.push(`- ${emoji} **${t.title}**${projectRef}`);
      if (t.requested_by) {
        lines.push(`  Requested by: ${t.requested_by}`);
      }
    }
    lines.push("");
  }

  if (data.counts.upcoming_events > 0 && data.events.length > 0) {
    lines.push("## Upcoming Events (7 days)");
    for (const e of data.events) {
      const start = formatEventTime(e.start_time);
      const venue = e.venue ? ` @ ${e.venue}` : "";
      lines.push(`- ${start}: **${e.title}**${venue}`);
    }
    lines.push("");
  }

  lines.push("## Summary");
  lines.push(`- Active projects: ${data.counts.active_projects}`);
  lines.push(`- Active tasks: ${data.counts.active_tasks}`);
  lines.push(`- Upcoming events: ${data.counts.upcoming_events}`);
  lines.push("");

  return lines.join("\n");
}

/**
 * Format event time for display.
 */
function formatEventTime(time: string): string {
  try {
    const date = new Date(time);
    return date.toISOString().slice(0, 16).replace("T", " ");
  } catch {
    return time;
  }
}

/**
 * Fetch context from Cyborg service.
 */
async function fetchCyborgContext(
  url: string,
): Promise<CyborgContextResponse | null> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 5000);

  try {
    const response = await fetch(`${url}/api/v1/context/summary`, {
      signal: controller.signal,
      headers: {
        Accept: "application/json",
      },
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      return null;
    }

    const data = (await response.json()) as CyborgContextResponse;
    return data;
  } catch (error) {
    clearTimeout(timeoutId);
    return null;
  }
}

/**
 * Create the Cyborg context engine instance.
 */
function createCyborgContextEngine(config: CyborgContextConfig): ContextEngine {
  // In-memory cache per session
  const cache = new Map<string, CyborgContextCache>();

  return {
    info: {
      id: "cyborg-context",
      name: "Cyborg Context",
      version: "0.1.0",
    },

    /**
     * Bootstrap - not needed for this engine, Cyborg is stateless.
     */
    async bootstrap() {
      return { bootstrapped: false, reason: "Cyborg context is stateless, no bootstrap needed" };
    },

    /**
     * Ingest messages - no-op for this engine, we only provide context on assemble.
     */
    async ingest() {
      return { ingested: false };
    },

    /**
     * Assemble the context for each model turn.
     * This is the key method that injects Cyborg context.
     */
    async assemble({ sessionId, messages, tokenBudget, model }) {
      if (!config.enabled) {
        return { messages, estimatedTokens: 0 };
      }

      // Check cache
      const now = Date.now();
      const cached = cache.get(sessionId);
      const shouldRefresh =
        !cached ||
        now - cached.timestamp > config.cacheTtlSeconds * 1000;

      let cyborgContext: CyborgContextResponse | null;

      if (shouldRefresh) {
        cyborgContext = await fetchCyborgContext(config.cyborgUrl);
        cache.set(sessionId, {
          context: cyborgContext,
          timestamp: now,
          text: cyborgContext ? formatCyborgContext(cyborgContext) : "",
        });
      } else {
        cyborgContext = cached.context;
      }

      if (!cyborgContext) {
        // Service unavailable - skip injection
        return { messages, estimatedTokens: 0 };
      }

      const contextText = formatCyborgContext(cyborgContext);
      const contextTokens = estimateTokens(contextText);

      // If context would exceed budget, skip or truncate
      if (contextTokens > config.maxTokens) {
        // Could implement smarter truncation here
        // For now, just skip if it doesn't fit
        return { messages, estimatedTokens: 0 };
      }

      // Create system message with Cyborg context
      const cyborgMessage: AgentMessage = {
        role: "system",
        content: contextText,
        timestamp: now,
      };

      return {
        messages: [cyborgMessage, ...messages],
        estimatedTokens: messages.length + contextTokens,
        systemPromptAddition: contextText,
      };
    },

    /**
     * Compact - no-op for this engine, context is refreshed per assemble.
     */
    async compact() {
      return { ok: true, compacted: false };
    },

    /**
     * Maintain - clear stale cache entries.
     */
    async maintain() {
      const now = Date.now();
      const ttlMs = config.cacheTtlSeconds * 1000;
      let cleared = 0;

      for (const [sessionId, cached] of cache.entries()) {
        if (now - cached.timestamp > ttlMs) {
          cache.delete(sessionId);
          cleared++;
        }
      }

      return {
        changed: false,
        bytesFreed: 0,
        rewrittenEntries: cleared,
        reason: cleared > 0 ? `Cleared ${cleared} stale cache entries` : undefined,
      };
    },

    /**
     * Dispose - clear cache.
     */
    async dispose() {
      cache.clear();
    },
  };
}

/**
 * Main plugin entry point.
 */
const cyborgPlugin = {
  id: "cyborg-context",
  name: "Cyborg Context",
  description: "Inject Bob's Cyborg context (projects, tasks, events) into conversations",

  configSchema: {
    parse(value: unknown) {
      const defaults: CyborgContextConfig = {
        enabled: true,
        cyborgUrl: "http://127.0.0.1:8420",
        includeProjects: true,
        includeTasks: true,
        includeEvents: true,
        cacheTtlSeconds: 300,
        maxTokens: 2000,
      };

      if (!value || typeof value !== "object") {
        return {
          ok: true,
          data: defaults,
        };
      }

      const cfg = value as Record<string, unknown>;
      const parsed: Partial<CyborgContextConfig> = {};

      if (typeof cfg.enabled === "boolean") {
        parsed.enabled = cfg.enabled;
      }
      if (typeof cfg.cyborgUrl === "string") {
        parsed.cyborgUrl = cfg.cyborgUrl.trim();
      }
      if (typeof cfg.includeProjects === "boolean") {
        parsed.includeProjects = cfg.includeProjects;
      }
      if (typeof cfg.includeTasks === "boolean") {
        parsed.includeTasks = cfg.includeTasks;
      }
      if (typeof cfg.includeEvents === "boolean") {
        parsed.includeEvents = cfg.includeEvents;
      }
      if (typeof cfg.cacheTtlSeconds === "number" && cfg.cacheTtlSeconds > 0) {
        parsed.cacheTtlSeconds = cfg.cacheTtlSeconds;
      }
      if (typeof cfg.maxTokens === "number" && cfg.maxTokens >= 0) {
        parsed.maxTokens = cfg.maxTokens;
      }

      return {
        ok: true,
        data: { ...defaults, ...parsed } as CyborgContextConfig,
      };
    },
  },

  register(api) {
    const config = parseCyborgConfig(api.pluginConfig);
    const engine = createCyborgContextEngine(config);

    api.registerContextEngine("cyborg-context", () => engine);

    if (config.enabled) {
      api.logger.info(
        `[cyborg] Plugin loaded (enabled=${config.enabled}, url=${config.cyborgUrl}, ttl=${config.cacheTtlSeconds}s, maxTokens=${config.maxTokens})`,
      );
    } else {
      api.logger.info("[cyborg] Plugin loaded (disabled)");
    }
  },
};

export default cyborgPlugin;
