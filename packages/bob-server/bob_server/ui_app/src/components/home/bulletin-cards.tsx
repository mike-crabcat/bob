import type { BulletinItem } from "@/routes";
import { Link } from "@tanstack/react-router";

interface Props {
  bulletins: BulletinItem[];
}

function relativeTime(iso: string): string {
  if (!iso) return "--";
  try {
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "now";
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    return `${Math.floor(hours / 24)}d ago`;
  } catch {
    return "--";
  }
}

function shortChannel(channelId: string): string {
  if (!channelId) return "";
  const parts = channelId.split(":");
  return parts.length > 2 ? parts.slice(-2).join(":") : channelId;
}

export function BulletinCards({ bulletins }: Props) {
  if (bulletins.length === 0) {
    return <div className="text-xs text-muted text-center py-3">no bulletins</div>;
  }

  return (
    <div className="flex flex-col gap-1">
      {bulletins.map((b) => (
        <Link key={b.id} to="/memory/bulletins/$bulletinId" params={{ bulletinId: b.id }} className="bg-surface border border-border p-2 block hover:border-accent/30 transition-colors">
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-accent truncate">{shortChannel(b.channel_id)}</span>
            <span className="text-[10px] text-muted shrink-0 ml-auto">{relativeTime(b.created_at)}</span>
            <span className="text-[10px] text-muted shrink-0 tabular-nums">{b.content_length.toLocaleString()} chars</span>
          </div>
        </Link>
      ))}
    </div>
  );
}
