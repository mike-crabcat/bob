import type React from "react";
import type { SummaryItem } from "@/routes";
import { RichText } from "@/components/shared/rich-text";

interface Props {
  summaries: SummaryItem[];
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

function shortKey(key: string): string {
  // Extract the meaningful part after the last colon-separated namespace
  const parts = key.split(":");
  // Keep last 2 segments if there are more than 2
  return parts.length > 2 ? parts.slice(-2).join(":") : key;
}

export function SummaryCards({ summaries }: Props) {
  if (summaries.length === 0) {
    return <div className="text-xs text-muted text-center py-3">no summaries</div>;
  }

  return (
    <div className="flex flex-col gap-1">
      {summaries.map((s) => (
        <div key={s.id} className="bg-surface border border-border p-2">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[11px] text-accent truncate">{shortKey(s.session_key)}</span>
            <span className="text-[10px] text-muted shrink-0">{relativeTime(s.created_at)}</span>
          </div>
          <div className="text-xs text-text leading-relaxed"><RichText text={s.summary_text} /></div>
          {s.topics.length > 0 && (
            <div className="text-[10px] text-muted mt-1">{s.topics.map((t, i) => <RichText key={i} text={t} />).reduce<React.ReactNode[]>((acc, el, i) => i === 0 ? [el] : [...acc, ", ", el], [])}</div>
          )}
        </div>
      ))}
    </div>
  );
}
