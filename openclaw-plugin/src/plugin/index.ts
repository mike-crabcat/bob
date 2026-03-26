/**
 * @cyborg/openclaw-plugin — Cyborg Context Plugin for OpenClaw
 *
 * Injects Bob's active projects, tasks, and events via before_prompt_build hook.
 */

import type { PluginHookAgentContext } from "openclaw/plugin-sdk";

type CyborgContextConfig = {
  enabled: boolean;
  cyborgUrl: string;
};

type CyborgContextResponse = {
  generated_at: string;
  project_counts: {
    active: number;
    closed: number;
    planning: number;
  };
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

/**
 * Parse plugin config from OpenClaw's runtime config.
 */
function parseCyborgConfig(
  config: unknown,
): CyborgContextConfig {
  const defaults: CyborgContextConfig = {
    enabled: true,
    cyborgUrl: "http://127.0.0.1:8420",
  };

  if (!config || typeof config !== "object") {
    return defaults;
  }

  const cfg = config as Record<string, unknown>;
  return {
    enabled: typeof cfg.enabled === "boolean" ? cfg.enabled : defaults.enabled,
    cyborgUrl:
      typeof cfg.cyborgUrl === "string" ? cfg.cyborgUrl.trim() : defaults.cyborgUrl,
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

  if (data.project_counts.active > 0 && data.projects.length > 0) {
    lines.push("## Active Projects");
    for (const p of data.projects) {
      const aim = p.aim ? `: ${p.aim}` : "";
      lines.push(`- **${p.title}**${aim}`);
    }
    lines.push("");
  }

  if (data.task_counts.active > 0 && data.tasks.length > 0) {
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
      const start = formatTime(e.start_time);
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
 * Format time for display.
 */
function formatTime(time: string): string {
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
 * Main plugin entry point.
 */
const cyborgPlugin = {
  id: "cyborg-context",
  name: "Cyborg Context",
  description: "Inject Bob's Cyborg context (projects, tasks, events) via before_prompt_build hook",

  configSchema: {
    parse(value: unknown) {
      const defaults: CyborgContextConfig = {
        enabled: true,
        cyborgUrl: "http://127.0.0.1:8420",
      };

      if (!value || typeof value !== "object") {
        return {
          ok: true,
          data: defaults,
        };
      }

      const cfg = value as Record<string, unknown>;
      return {
        enabled: typeof cfg.enabled === "boolean" ? cfg.enabled : defaults.enabled,
        cyborgUrl:
          typeof cfg.cyborgUrl === "string" ? cfg.cyborgUrl.trim() : defaults.cyborgUrl,
      };
    },
  },

  register(api) {
    const config = parseCyborgConfig(api.pluginConfig);

    // Register before_prompt_build hook to contribute Cyborg context
    api.registerHook({
      name: "cyborg-context-prepend",
      entry: "before_prompt_build",
      async handler(ctx) {
        api.logger.debug(
          "[cyborg] before_prompt_build hook called",
          { sessionId: ctx.sessionId, enabled: config.enabled },
        );

        if (!config.enabled) {
          return undefined;
        }

        const cyborgContext = await fetchCyborgContext(config.cyborgUrl);
        if (!cyborgContext) {
          return undefined;
        }

        const contextText = formatCyborgContext(cyborgContext);
        api.logger.debug(
          "[cyborg] Prepending context:",
          { contextLength: contextText.length },
        );

        return { prependContext: contextText };
      },
    });

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
