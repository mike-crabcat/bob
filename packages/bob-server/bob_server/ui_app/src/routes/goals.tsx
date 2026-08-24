import { createFileRoute, Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { fetchAPI, postAPI } from "@/lib/api";

interface GoalTransition {
  from_status: string | null;
  to_status: string;
  note: string | null;
  created_at: string;
}

interface NextAction {
  action: string;
  due: string;
}

interface GoalState {
  plan: string;
  known: number;
  open_questions: string[];
  next_actions: NextAction[];
  entities: string[];
}

interface Goal {
  id: string;
  conversation_id: string;
  origin_conversation_id: string | null;
  parent_goal_id: string | null;
  children: string[];
  kind: string;
  objective: string;
  progress: string | null;
  result: string;
  status: string;
  deadline: string | null;
  created_at: string;
  updated_at: string;
  state?: GoalState;
  transitions: GoalTransition[];
}

interface RoutingDecision {
  id: string;
  stimulus_id: string;
  source_conversation_id: string;
  goal_id: string;
  match_type: string;
  probe_verdict: string | null;
  revise_outcome: string | null;
  wake_decision: string | null;
  created_at: string;
}

interface Wakeup {
  id: string;
  conversation_id: string;
  goal_id: string | null;
  kind: string;
  not_before: string;
  recurrence: string | null;
  tz: string | null;
  status: string;
  payload: string;
}

const STATUS_COLOR: Record<string, string> = {
  active: "text-success",
  completed: "text-accent",
  failed: "text-error",
  cancelled: "text-muted",
};

function fmtTs(iso: string | null): string {
  if (!iso) return "--";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function Countdown({ iso }: { iso: string }) {
  const diff = new Date(iso).getTime() - Date.now();
  if (isNaN(diff)) return <span className="text-muted">--</span>;
  if (diff < 0) return <span className="text-yellow-400">due</span>;
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return <span className="text-text">{mins}m</span>;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return <span className="text-text">{hours}h {mins % 60}m</span>;
  return <span className="text-text">{Math.floor(hours / 24)}d</span>;
}

function GoalCard({ goal }: { goal: Goal }) {
  const [open, setOpen] = useState(false);
  const qc = useQueryClient();
  const cancel = useMutation({
    mutationFn: () => postAPI(`/goals/${goal.id}/cancel`, {}),
    onSettled: () => qc.invalidateQueries({ queryKey: ["goals"] }),
  });

  return (
    <div className="bg-surface border border-border">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-start gap-2 p-2 text-left"
      >
        <span className={`text-[9px] uppercase mt-0.5 shrink-0 ${STATUS_COLOR[goal.status] ?? "text-muted"}`}>
          {goal.status}
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-xs text-text break-words">{goal.objective}</div>
          <div className="text-[10px] text-muted mt-0.5">
            {goal.kind}
            {goal.deadline && <> · due {fmtTs(goal.deadline)}</>}
            {" · "}updated {fmtTs(goal.updated_at)}
          </div>
        </div>
        <span className="text-muted text-xs shrink-0">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="border-t border-border p-2 text-[11px] flex flex-col gap-2">
          <div className="text-muted break-all">
            conversation:{" "}
            <Link
              to="/conversations/$sessionKey"
              params={{ sessionKey: goal.conversation_id }}
              className="text-accent hover:underline"
            >
              {goal.conversation_id}
            </Link>
            {goal.parent_goal_id && <> · child of <span className="text-accent">{goal.parent_goal_id.slice(0, 8)}</span></>}
            {goal.children.length > 0 && <> · {goal.children.length} child goal{goal.children.length > 1 ? "s" : ""}</>}
          </div>
          {goal.state && (
            <div className="border border-border p-1.5 flex flex-col gap-1">
              {goal.state.plan && <div className="text-text">{goal.state.plan}</div>}
              {goal.state.next_actions.length > 0 && (
                <div>
                  <div className="text-[9px] uppercase text-muted mb-0.5">next actions</div>
                  {goal.state.next_actions.map((na, i) => (
                    <div key={i} className="text-text">
                      · {na.action}{na.due && <span className="text-muted"> (due {fmtTs(na.due)})</span>}
                    </div>
                  ))}
                </div>
              )}
              {goal.state.open_questions.length > 0 && (
                <div>
                  <div className="text-[9px] uppercase text-muted mb-0.5">open</div>
                  {goal.state.open_questions.map((q, i) => (
                    <div key={i} className="text-muted">? {q}</div>
                  ))}
                </div>
              )}
              {goal.state.entities.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {goal.state.entities.map((e) => (
                    <span key={e} className="border border-border px-1 text-[10px] text-muted">{e}</span>
                  ))}
                </div>
              )}
              <div className="text-[10px] text-muted">{goal.state.known} known fact(s)</div>
            </div>
          )}
          {goal.progress && <div className="text-text whitespace-pre-wrap">{goal.progress}</div>}
          {goal.result && <div className="text-muted whitespace-pre-wrap">{goal.result}</div>}
          {goal.transitions.length > 0 && (
            <div>
              <div className="text-[9px] uppercase text-muted mb-0.5">history</div>
              {goal.transitions.map((t, i) => (
                <div key={i} className="text-muted">
                  {fmtTs(t.created_at)} · {t.from_status ?? "·"}→{t.to_status}
                  {t.note && ` — ${t.note}`}
                </div>
              ))}
            </div>
          )}
          {goal.status === "active" && (
            <button
              className="self-start border border-border px-2 py-0.5 text-muted hover:bg-border"
              disabled={cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              cancel goal
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function GoalsPage() {
  const qc = useQueryClient();
  const { data: goalsData } = useQuery<{ goals: Goal[] }>({
    queryKey: ["goals"],
    queryFn: () => fetchAPI<{ goals: Goal[] }>("/goals"),
    refetchInterval: 30_000,
  });
  const { data: wakeupsData } = useQuery<{ scheduled: Wakeup[]; recent: Wakeup[] }>({
    queryKey: ["wakeups"],
    queryFn: () => fetchAPI<{ scheduled: Wakeup[]; recent: Wakeup[] }>("/wakeups"),
    refetchInterval: 30_000,
  });
  const cancelWakeup = useMutation({
    mutationFn: (id: string) => postAPI(`/wakeups/${id}/cancel`, {}),
    onSettled: () => qc.invalidateQueries({ queryKey: ["wakeups"] }),
  });

  const goals = goalsData?.goals ?? [];
  const active = goals.filter((g) => g.status === "active");
  const settled = goals.filter((g) => g.status !== "active");
  const scheduled = wakeupsData?.scheduled ?? [];
  const { data: routingData } = useQuery<{ decisions: RoutingDecision[] }>({
    queryKey: ["routing-log"],
    queryFn: () => fetchAPI<{ decisions: RoutingDecision[] }>("/memory/routing-log"),
    refetchInterval: 30_000,
  });
  const routing = routingData?.decisions ?? [];

  // Goal tree: roots are goals whose parent isn't in the visible window.
  const byId = new Map(goals.map((g) => [g.id, g]));
  const isChild = (g: Goal) => !!g.parent_goal_id && byId.has(g.parent_goal_id);
  const roots = active.filter((g) => !isChild(g));

  return (
    <div className="flex flex-col gap-4 p-3">
      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">
          active goals ({active.length})
        </h2>
        <div className="flex flex-col gap-1.5">
          {active.length === 0 && (
            <div className="text-xs text-muted text-center py-2">no active goals</div>
          )}
          {roots.map((g) => (
            <div key={g.id} className="flex flex-col gap-1.5">
              <GoalCard goal={g} />
              <div className="pl-3 flex flex-col gap-1.5 border-l border-border">
                {g.children
                  .map((cid) => byId.get(cid))
                  .filter((c): c is Goal => !!c && c.status === "active")
                  .map((c) => <GoalCard key={c.id} goal={c} />)}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">
          scheduled wakeups ({scheduled.length})
        </h2>
        <div className="bg-surface border border-border divide-y divide-border text-[11px]">
          {scheduled.length === 0 && (
            <div className="p-2 text-muted text-center">none scheduled</div>
          )}
          {scheduled.map((w) => (
            <div key={w.id} className="flex items-center gap-2 px-2 py-1.5">
              <span className="uppercase text-[9px] text-accent shrink-0">{w.kind}</span>
              <div className="min-w-0 flex-1">
                <Link
                  to="/conversations/$sessionKey"
                  params={{ sessionKey: w.conversation_id }}
                  className="text-text hover:underline truncate block"
                >
                  {w.conversation_id}
                </Link>
                <div className="text-[10px] text-muted">
                  {fmtTs(w.not_before)}
                  {w.recurrence && ` · ${w.recurrence}`}
                  {w.tz && ` · ${w.tz}`}
                </div>
              </div>
              <span className="shrink-0 tabular-nums text-[10px]">
                <Countdown iso={w.not_before} />
              </span>
              <button
                className="border border-border px-1.5 py-0.5 text-muted hover:bg-border shrink-0"
                disabled={cancelWakeup.isPending}
                onClick={() => cancelWakeup.mutate(w.id)}
              >
                cancel
              </button>
            </div>
          ))}
        </div>
      </section>

      {settled.length > 0 && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">
            settled goals ({settled.length})
          </h2>
          <div className="flex flex-col gap-1.5">
            {settled.map((g) => <GoalCard key={g.id} goal={g} />)}
          </div>
        </section>
      )}

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">
          memory routing ({routing.length})
        </h2>
        <div className="bg-surface border border-border divide-y divide-border text-[11px]">
          {routing.length === 0 && (
            <div className="p-2 text-muted text-center">no routing decisions yet</div>
          )}
          {routing.map((r) => (
            <div key={r.id} className="flex items-center gap-2 px-2 py-1.5">
              <span className="text-[9px] uppercase text-accent shrink-0">{r.match_type}</span>
              <div className="min-w-0 flex-1">
                <Link
                  to="/conversations/$sessionKey"
                  params={{ sessionKey: r.source_conversation_id }}
                  className="text-text hover:underline truncate block"
                >
                  {r.source_conversation_id}
                </Link>
                <div className="text-[10px] text-muted">
                  → goal {r.goal_id.slice(0, 8)} · {fmtTs(r.created_at)}
                </div>
              </div>
              <span className="shrink-0 text-[10px] text-muted">
                {r.probe_verdict && r.probe_verdict !== "skipped" ? `probe:${r.probe_verdict} ` : ""}
                {r.revise_outcome ?? "?"} / {r.wake_decision ?? "?"}
              </span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

export const Route = createFileRoute("/goals")({ component: GoalsPage });
