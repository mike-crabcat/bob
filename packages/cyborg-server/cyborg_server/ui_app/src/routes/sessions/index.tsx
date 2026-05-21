import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { fetchAPI } from "@/lib/api";

interface SessionItem {
  session_key: string;
  channel: string;
  call_count: number;
  completed: number;
  failed: number;
  avg_latency: number;
  last_activity: string;
}

interface SessionsSnapshot {
  sessions: SessionItem[];
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

function SessionsPage() {
  const [filter, setFilter] = useState<string>("all");

  const { data } = useQuery<SessionsSnapshot>({
    queryKey: ["sessions"],
    queryFn: () => fetchAPI<SessionsSnapshot>("/sessions"),
  });

  const sessions = data?.sessions ?? [];
  const filtered = filter === "all" ? sessions : sessions.filter((s) => s.channel === filter);
  const channels = ["all", ...Array.from(new Set(sessions.map((s) => s.channel)))];

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
          <div className="p-4 text-muted text-center text-xs">no sessions</div>
        ) : (
          filtered.map((s) => (
            <Link
              key={s.session_key}
              to="/sessions/$sessionKey"
              params={{ sessionKey: s.session_key }}
              className="flex items-center gap-2 px-3 py-2 border-b border-border hover:bg-surface transition-colors"
            >
              <div className="flex flex-col items-start gap-0.5 min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <ChannelDot channel={s.channel} />
                  <span className={`text-[10px] uppercase ${CHANNEL_COLORS[s.channel] ?? "text-muted"}`}>
                    {s.channel}
                  </span>
                  <span className="text-text truncate text-xs">{s.session_key}</span>
                </div>
                <div className="text-[10px] text-muted">
                  {s.call_count} calls · {s.failed} failed · avg {s.avg_latency}s
                </div>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <RelativeTime iso={s.last_activity} />
                <span className="text-muted text-xs">&rsaquo;</span>
              </div>
            </Link>
          ))
        )}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/sessions/")({ component: SessionsPage });
