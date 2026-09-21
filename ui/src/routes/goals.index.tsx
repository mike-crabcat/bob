import { createFileRoute, Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchAPI, postAPI } from "@/lib/api";
import { parseTs } from "@/lib/time";

interface GoalTransition {
  from_status: string | null;
  to_status: string;
  note: string | null;
  created_at: string;
}

interface NextRun {
  at: string;
  frame: string | null;
}

interface LoopSummary {
  on: boolean;
  budget_total: number | null;
  budget_spent: number | null;
  stall_streak: number;
  skip_streak: number;
  open_tasks: number;
  next_run: NextRun | null;
}

interface Goal {
  id: string;
  conversation_id: string;
  origin_conversation_id: string | null;
  kind: string;
  objective: string;
  status: string;
  deadline: string | null;
  created_at: string;
  updated_at: string;
  children: string[];
  loop: LoopSummary;
  transitions: GoalTransition[];
}

interface GoalsSnapshot {
  goals: Goal[];
}

const STATUS_COLOR: Record<string, string> = {
  active: "text-green-500",
  completed: "text-blue-400",
  cancelled: "text-muted",
  failed: "text-red-400",
};

function fmtTs(ts: string | null | undefined): string {
  if (!ts) return "—";
  return parseTs(ts).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function untilTs(ts: string): string {
  const diffMs = parseTs(ts).getTime() - Date.now();
  if (diffMs <= 0) return "due now";
  const mins = Math.round(diffMs / 60000);
  if (mins < 90) return `in ${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 36) return `in ${hours}h`;
  return `in ${Math.round(hours / 24)}d`;
}

function LoopLine({ loop }: { loop: LoopSummary }) {
  if (!loop.on) return null;
  const parts: string[] = [];
  if (loop.next_run) {
    parts.push(`next ${untilTs(loop.next_run.at)}`);
  } else if (loop.open_tasks > 0) {
    parts.push(`waiting on ${loop.open_tasks} branch${loop.open_tasks > 1 ? "es" : ""}`);
  } else {
    parts.push("idle — dead-man");
  }
  if (loop.budget_total) {
    parts.push(`budget ${loop.budget_spent ?? 0}/${loop.budget_total}`);
  }
  const flags: string[] = [];
  if (loop.stall_streak > 0) flags.push(`stalled ×${loop.stall_streak}`);
  if (loop.skip_streak > 3) flags.push(`skips ×${loop.skip_streak}`);
  return (
    <span className="text-[10px] text-muted">
      {" · "}
      {parts.join(" · ")}
      {flags.length > 0 && <span className="text-amber-500"> · {flags.join(" · ")}</span>}
    </span>
  );
}

function GoalRow({ goal }: { goal: Goal }) {
  return (
    <Link
      to="/goals/$goalId"
      params={{ goalId: goal.id }}
      className="block bg-surface border border-border p-2 hover:border-accent"
    >
      <div className="flex items-start gap-2">
        <span className={`text-[9px] uppercase mt-0.5 shrink-0 ${STATUS_COLOR[goal.status] ?? "text-muted"}`}>
          {goal.status}
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-xs text-text break-words">{goal.objective}</div>
          <div className="text-[10px] text-muted mt-0.5">
            {goal.kind}
            {goal.deadline && <> · due {fmtTs(goal.deadline)}</>}
            {" · "}updated {fmtTs(goal.updated_at)}
            <LoopLine loop={goal.loop} />
          </div>
        </div>
        <span className="text-muted text-[10px] shrink-0">{goal.id.slice(0, 8)}</span>
      </div>
    </Link>
  );
}

function Goals() {
  const { data, isLoading } = useQuery({
    queryKey: ["goals"],
    queryFn: () => fetchAPI<GoalsSnapshot>("/goals"),
    refetchInterval: 15000,
  });
  const qc = useQueryClient();
  const cancel = useMutation({
    mutationFn: (id: string) => postAPI(`/goals/${id}/cancel`, {}),
    onSettled: () => qc.invalidateQueries({ queryKey: ["goals"] }),
  });

  if (isLoading) return <div className="p-4 text-muted text-xs">loading goals…</div>;
  const goals = data?.goals ?? [];
  const active = goals.filter((g) => g.status === "active");
  const settled = goals.filter((g) => g.status !== "active");

  return (
    <div className="p-4 flex flex-col gap-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-sm text-text">Goals</h1>
        <span className="text-[10px] text-muted">
          {active.length} active · {settled.length} settled
        </span>
      </div>

      <section className="flex flex-col gap-1">
        {active.length === 0 && (
          <div className="text-[11px] text-muted">No active goals.</div>
        )}
        {active.map((g) => (
          <div key={g.id} className="relative group">
            <GoalRow goal={g} />
            {g.status === "active" && (
              <button
                onClick={(e) => {
                  e.preventDefault();
                  if (confirm(`Cancel goal "${g.objective.slice(0, 60)}"?`)) cancel.mutate(g.id);
                }}
                className="absolute right-1 top-1 hidden group-hover:block text-[9px] uppercase text-red-400 border border-red-900 px-1"
              >
                cancel
              </button>
            )}
          </div>
        ))}
      </section>

      {settled.length > 0 && (
        <section className="flex flex-col gap-1">
          <div className="text-[9px] uppercase text-muted">settled</div>
          {settled.slice(0, 20).map((g) => (
            <GoalRow key={g.id} goal={g} />
          ))}
        </section>
      )}
    </div>
  );
}

export const Route = createFileRoute("/goals/")({ component: Goals });
