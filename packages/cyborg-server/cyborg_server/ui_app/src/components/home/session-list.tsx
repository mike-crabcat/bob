import { Link } from "@tanstack/react-router";
import type { SessionItem } from "@/routes";

interface Props {
  sessions: SessionItem[];
}

function RelativeTime({ iso }: { iso: string }) {
  if (!iso) return <span className="text-[10px] text-muted">--</span>;
  try {
    const d = new Date(iso);
    const diff = Date.now() - d.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return <span className="text-[10px] text-success">now</span>;
    if (mins < 60) return <span className="text-[10px] text-muted">{mins}m ago</span>;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return <span className="text-[10px] text-muted">{hours}h ago</span>;
    return <span className="text-[10px] text-muted">{Math.floor(hours / 24)}d ago</span>;
  } catch {
    return <span className="text-[10px] text-muted">--</span>;
  }
}

const CHANNEL_COLORS: Record<string, string> = {
  whatsapp: "bg-whatsapp",
  email: "bg-email",
  voice: "bg-voice",
  other: "bg-muted",
};

export function SessionList({ sessions }: Props) {
  if (sessions.length === 0) {
    return <div className="text-xs text-muted text-center py-3">no active sessions</div>;
  }

  return (
    <div className="bg-surface border border-border divide-y divide-border">
      {sessions.map((s) => {
        const isRecent = s.last_activity && Date.now() - new Date(s.last_activity).getTime() < 300000;
        return (
          <Link
            key={s.session_key}
            to="/sessions/$sessionKey"
            params={{ sessionKey: s.session_key }}
            className="flex items-center gap-2 px-2 py-1.5 hover:bg-border transition-colors"
          >
            <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${isRecent ? "bg-success" : "bg-muted"}`} />
            <span className={`text-[10px] uppercase shrink-0 ${s.channel === "whatsapp" ? "text-whatsapp" : s.channel === "email" ? "text-email" : s.channel === "voice" ? "text-voice" : "text-muted"}`}>
              {s.channel}
            </span>
            <span className="text-xs text-text truncate flex-1">{s.session_key.split(":").slice(-2).join(":")}</span>
            <span className="text-[10px] text-muted shrink-0">{s.call_count} calls</span>
            <RelativeTime iso={s.last_activity} />
          </Link>
        );
      })}
    </div>
  );
}
