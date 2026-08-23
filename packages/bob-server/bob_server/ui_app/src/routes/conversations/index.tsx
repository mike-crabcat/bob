import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { fetchAPI } from "@/lib/api";

interface BindingChip {
  session_key: string;
  channel: string;
  kind: string;
  address: string | null;
  merged: boolean;
}

interface ConversationItem {
  id: string;
  kind: string;
  title: string | null;
  merged_into: string | null;
  channel: string;
  binding_count: number;
  bindings: BindingChip[];
  turn_count: number;
  active_goals: number;
  last_activity: string;
}

interface ConversationsSnapshot {
  conversations: ConversationItem[];
}

const CHANNEL_COLORS: Record<string, string> = {
  whatsapp: "text-whatsapp",
  email: "text-email",
  voice: "text-voice",
  other: "text-muted",
};

function RelativeTime({ iso }: { iso: string }) {
  if (!iso) return <span className="text-[10px] text-muted">--</span>;
  try {
    const d = new Date(iso);
    const diff = Date.now() - d.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return <span className="text-[10px] text-success">now</span>;
    if (mins < 60) return <span className="text-[10px] text-muted">{mins}m</span>;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return <span className="text-[10px] text-muted">{hours}h</span>;
    return <span className="text-[10px] text-muted">{Math.floor(hours / 24)}d</span>;
  } catch {
    return <span className="text-[10px] text-muted">--</span>;
  }
}

function ChannelDot({ channel }: { channel: string }) {
  const colors: Record<string, string> = {
    whatsapp: "bg-whatsapp",
    email: "bg-email",
    voice: "bg-voice",
    other: "bg-muted",
  };
  return <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${colors[channel] ?? "bg-muted"}`} />;
}

function ConversationsPage() {
  const [filter, setFilter] = useState<string>("all");

  const { data } = useQuery<ConversationsSnapshot>({
    queryKey: ["conversations"],
    queryFn: () => fetchAPI<ConversationsSnapshot>("/conversations"),
  });

  const conversations = data?.conversations ?? [];
  const filtered =
    filter === "all" ? conversations : conversations.filter((c) => c.channel === filter);
  const channels = ["all", ...Array.from(new Set(conversations.map((c) => c.channel)))];

  return (
    <div className="flex flex-col h-full">
      <div className="flex gap-1 px-3 py-2 border-b border-border overflow-x-auto shrink-0">
        {channels.map((ch) => (
          <button
            key={ch}
            onClick={() => setFilter(ch)}
            className={`px-2 py-1 text-[11px] border border-border shrink-0 transition-colors ${
              filter === ch ? "bg-accent text-bg" : "text-muted hover:text-text"
            }`}
          >
            {ch}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="p-4 text-muted text-center text-xs">no conversations</div>
        ) : (
          filtered.map((c) => (
            <Link
              key={c.id}
              to="/conversations/$sessionKey"
              params={{ sessionKey: c.id }}
              className="flex items-center gap-2 px-3 py-2 border-b border-border hover:bg-surface transition-colors"
            >
              <div className="flex flex-col items-start gap-0.5 min-w-0 flex-1">
                <div className="flex items-center gap-1.5 min-w-0 w-full">
                  <ChannelDot channel={c.channel} />
                  <span className={`text-[10px] uppercase ${CHANNEL_COLORS[c.channel] ?? "text-muted"}`}>
                    {c.channel}
                  </span>
                  <span className="text-text truncate text-xs">{c.title || c.id}</span>
                  {c.binding_count > 1 && (
                    <span className="text-[9px] px-1 border border-accent/60 text-accent shrink-0">
                      ×{c.binding_count}
                    </span>
                  )}
                  {c.bindings.some((b) => b.merged) && (
                    <span className="text-[9px] px-1 border border-yellow-500/60 text-yellow-400 shrink-0">
                      merged
                    </span>
                  )}
                </div>
                <div className="text-[10px] text-muted">
                  {c.kind} · {c.turn_count} turns
                  {c.active_goals > 0 && ` · ${c.active_goals} goals`}
                  {c.binding_count > 1 &&
                    ` · ${Array.from(new Set(c.bindings.map((b) => b.channel))).join("+")}`}
                </div>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <RelativeTime iso={c.last_activity} />
                <span className="text-muted text-xs">&rsaquo;</span>
              </div>
            </Link>
          ))
        )}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/conversations/")({ component: ConversationsPage });
