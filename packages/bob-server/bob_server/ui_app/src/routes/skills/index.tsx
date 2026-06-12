import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { fetchAPI } from "@/lib/api";

interface InstalledSkill {
  name: string;
  description: string;
  trigger: string;
  has_helper: boolean;
  has_pyproject: boolean;
}

interface Delegation {
  id: string;
  session_key: string;
  user_story: string;
  plan_preview: string;
  status: string;
  files_created: string[];
  result_summary: string | null;
  cost_usd: number;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

interface SkillsSnapshot {
  skills: InstalledSkill[];
}

interface DelegationsSnapshot {
  delegations: Delegation[];
}

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

function StatusBadge({ status }: { status: string }) {
  const active = status === "planning" || status === "implementing";
  const colors: Record<string, string> = {
    planning: "bg-accent/20 text-accent",
    implementing: "bg-accent/20 text-accent",
    plan_ready: "bg-accent/20 text-accent",
    completed: "bg-success/20 text-success",
    failed: "bg-error/20 text-error",
    rejected: "bg-muted/20 text-muted",
  };
  return (
    <span className={`text-[10px] px-1.5 py-0.5 ${colors[status] ?? "bg-muted/20 text-muted"} ${active ? "animate-pulse" : ""}`}>
      {status.replace("_", " ")}
    </span>
  );
}

const STATUS_FILTERS = ["all", "active", "plan_ready", "completed", "failed", "rejected"] as const;

function SkillsPage() {
  const [filter, setFilter] = useState<string>("all");

  const { data: skillsData } = useQuery<SkillsSnapshot>({
    queryKey: ["skills-installed"],
    queryFn: () => fetchAPI<SkillsSnapshot>("/skills/installed"),
  });

  const { data: delegationsData } = useQuery<DelegationsSnapshot>({
    queryKey: ["skills-delegations"],
    queryFn: () => fetchAPI<DelegationsSnapshot>("/skills/delegations"),
  });

  const skills = skillsData?.skills ?? [];
  const delegations = delegationsData?.delegations ?? [];

  const filtered = filter === "all"
    ? delegations
    : filter === "active"
      ? delegations.filter((d) => d.status === "planning" || d.status === "implementing")
      : delegations.filter((d) => d.status === filter);

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-border">
        <h1 className="text-xs font-sans uppercase tracking-wider text-muted">installed skills</h1>
      </div>

      <div className="overflow-y-auto">
        {skills.length === 0 ? (
          <div className="px-3 py-2 text-[10px] text-muted">no skills installed</div>
        ) : (
          skills.map((s) => (
            <div key={s.name} className="px-3 py-2 border-b border-border">
              <div className="flex items-center gap-2">
                <span className="text-xs font-medium text-text">{s.name}</span>
                {s.has_helper && <span className="w-1.5 h-1.5 rounded-full bg-success shrink-0" title="has helper.py" />}
              </div>
              {s.description && <div className="text-[10px] text-muted mt-0.5">{s.description}</div>}
              {s.trigger && <div className="text-[10px] text-muted/60 mt-0.5 truncate">trigger: {s.trigger}</div>}
            </div>
          ))
        )}
      </div>

      <div className="flex gap-1 px-3 py-2 border-b border-border shrink-0">
        <h2 className="text-xs font-sans uppercase tracking-wider text-muted mr-auto self-center">developments</h2>
      </div>

      <div className="flex gap-1 px-3 py-1.5 border-b border-border overflow-x-auto shrink-0">
        {STATUS_FILTERS.map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-2 py-1 text-[11px] border border-border shrink-0 transition-colors ${
              filter === f ? "bg-accent text-bg" : "text-muted hover:text-text"
            }`}
          >
            {f === "plan_ready" ? "ready" : f}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="p-4 text-muted text-center text-xs">no delegations</div>
        ) : (
          filtered.map((d) => (
            <Link
              key={d.id}
              to="/skills/$delegationId"
              params={{ delegationId: d.id }}
              className="flex items-center gap-2 px-3 py-2 border-b border-border hover:bg-surface transition-colors"
            >
              <div className="flex flex-col gap-0.5 min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <StatusBadge status={d.status} />
                  <span className="text-xs text-text truncate">{d.user_story.slice(0, 80)}</span>
                </div>
                <div className="text-[10px] text-muted">
                  {d.files_created.length > 0 && `${d.files_created.join(", ")} · `}
                  {d.cost_usd > 0 && `$${d.cost_usd.toFixed(4)} · `}
                  <RelativeTime iso={d.created_at} />
                </div>
              </div>
              <span className="text-muted text-xs shrink-0">&rsaquo;</span>
            </Link>
          ))
        )}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/skills/")({ component: SkillsPage });
