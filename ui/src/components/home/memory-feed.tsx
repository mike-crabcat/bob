import type { MemoryFeedItem } from "@/routes";
import { Link } from "@tanstack/react-router";

interface Props {
  items: MemoryFeedItem[];
}

const CLAIM_COLORS: Record<string, string> = {
  spouse: "bg-pink-900/40 text-pink-300",
  parent: "bg-pink-900/40 text-pink-300",
  child: "bg-pink-900/40 text-pink-300",
  sibling: "bg-pink-900/40 text-pink-300",
  home_address: "bg-cyan-900/40 text-cyan-300",
  workplace: "bg-cyan-900/40 text-cyan-300",
  job: "bg-cyan-900/40 text-cyan-300",
  food_preference: "bg-orange-900/40 text-orange-300",
  drink_preference: "bg-orange-900/40 text-orange-300",
  interest: "bg-purple-900/40 text-purple-300",
  personality: "bg-purple-900/40 text-purple-300",
  language: "bg-blue-900/40 text-blue-300",
  birthday: "bg-blue-900/40 text-blue-300",
  alias: "bg-gray-900/40 text-gray-300",
  contact_id: "bg-gray-900/40 text-gray-300",
  member: "bg-green-900/40 text-green-300",
  destination: "bg-green-900/40 text-green-300",
  start_date: "bg-blue-900/40 text-blue-300",
  end_date: "bg-blue-900/40 text-blue-300",
  task_status: "bg-yellow-900/40 text-yellow-300",
  owner: "bg-yellow-900/40 text-yellow-300",
  due_date: "bg-yellow-900/40 text-yellow-300",
  description: "bg-gray-900/40 text-gray-300",
  location: "bg-cyan-900/40 text-cyan-300",
  transport_type: "bg-cyan-900/40 text-cyan-300",
  decision: "bg-green-900/40 text-green-300",
  rationale: "bg-green-900/40 text-green-300",
  purpose: "bg-indigo-900/40 text-indigo-300",
  name: "bg-blue-900/40 text-blue-300",
  stop: "bg-teal-900/40 text-teal-300",
  file_path: "bg-amber-900/40 text-amber-300",
  file_ref: "bg-amber-900/40 text-amber-300",
  thing_type: "bg-lime-900/40 text-lime-300",
  truth: "bg-rose-900/40 text-rose-300",
};

function relativeTime(iso: string | null): string {
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

export function MemoryFeed({ items }: Props) {
  if (items.length === 0) {
    return <div className="text-xs text-muted text-center py-3">no memory activity</div>;
  }

  return (
    <div className="flex flex-col gap-1">
      {items.map((c) => {
        const object = c.object_name ?? c.value;
        return (
          <Link
            key={c.id}
            to="/memory"
            search={{ entity: c.subject_id }}
            className="bg-surface border border-border p-2 block hover:border-accent/30 transition-colors"
          >
            <div className="flex items-center gap-2">
              <span className={`text-[10px] px-1 rounded shrink-0 ${CLAIM_COLORS[c.claim_type] ?? "bg-gray-900/40 text-gray-300"}`}>
                {c.claim_type}
              </span>
              <span className="text-[11px] text-text truncate flex-1">
                {c.subject_name}
                {object && <span className="text-muted"> → {object}</span>}
              </span>
              <span className="text-[10px] text-muted shrink-0 ml-auto tabular-nums">{relativeTime(c.created_at)}</span>
            </div>
          </Link>
        );
      })}
    </div>
  );
}
