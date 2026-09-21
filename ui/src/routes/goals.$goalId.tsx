import { createFileRoute, Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchAPI, postAPI } from "@/lib/api";
import { parseTs } from "@/lib/time";

interface StrategyBranch {
  id: string;
  hypothesis: string;
  status: string;
  verdict: string;
  first_step?: string;
}

interface GoalDetail {
  goal: {
    id: string;
    kind: string;
    objective: string;
    status: string;
    progress: string | null;
    result: string | null;
    deadline: string | null;
    conversation_id: string;
    origin_conversation_id: string | null;
    version: number | null;
    created_at: string;
    updated_at: string;
  };
  state: {
    plan: string;
    known: string[];
    open_questions: string[];
    next_actions: { action: string; due: string }[];
    strategies: StrategyBranch[];
    entities: string[];
  };
  loop: {
    on: boolean;
    budget_total: number | null;
    budget_spent: number | null;
    stall_streak: number;
    skip_streak: number;
    last_frame: string | null;
    last_delta: { spawned?: number; settled?: number; frame?: string | null } | null;
    next_run: { at: string; frame: string | null; note: string } | null;
  };
  branches: {
    id: string;
    title: string;
    status: string;
    completer: string | null;
    due: string | null;
    result: string;
    completed_at: string | null;
    created_at: string | null;
  }[];
  wakes: { id: string; kind: string; at: string | null; status: string; created_at: string | null }[];
  turns: { role: string; provenance: string | null; content: string; created_at: string | null }[];
}

const STRATEGY_MARK: Record<string, string> = {
  won: "✓", pruned: "✗", tested: "·", candidate: "?",
};

function fmtTs(ts: string | null | undefined): string {
  if (!ts) return "—";
  return parseTs(ts).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="bg-surface border border-border p-3 flex flex-col gap-2">
      <h2 className="text-[9px] uppercase text-muted">{title}</h2>
      {children}
    </section>
  );
}

function GoalDetailPage() {
  const { goalId } = Route.useParams();
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["goal", goalId],
    queryFn: () => fetchAPI<GoalDetail>(`/goals/${goalId}`),
    refetchInterval: 10000,
  });

  const pause = useMutation({
    mutationFn: () => postAPI(`/goals/${goalId}/pause`, {}),
    onSettled: () => qc.invalidateQueries({ queryKey: ["goal", goalId] }),
  });
  const nudge = useMutation({
    mutationFn: () =>
      postAPI(`/conversations/wake`, {
        target: data?.goal.conversation_id,
        instruction: "Operator nudge from the goal dashboard: run one round now.",
      }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["goal", goalId] }),
  });
  const cancel = useMutation({
    mutationFn: () => postAPI(`/goals/${goalId}/cancel`, {}),
    onSettled: () => qc.invalidateQueries({ queryKey: ["goal", goalId] }),
  });

  if (isLoading) return <div className="p-4 text-muted text-xs">loading goal…</div>;
  if (error || !data || "error" in (data as object))
    return <div className="p-4 text-red-400 text-xs">goal not found</div>;

  const { goal, state, loop, branches, wakes, turns } = data;
  const budgetPct =
    loop.budget_total && loop.budget_spent != null
      ? Math.min(100, Math.round((loop.budget_spent / loop.budget_total) * 100))
      : null;

  // merged timeline: wakes + room turns, newest first
  const timeline = [
    ...wakes.map((w) => ({
      at: w.at || w.created_at || "",
      kind: "wake" as const,
      label: `${w.kind} · ${w.status}`,
    })),
    ...turns
      .filter((t) => t.role === "assistant" || t.provenance)
      .map((t) => ({
        at: t.created_at || "",
        kind: t.role === "assistant" ? ("turn" as const) : ("stimulus" as const),
        label:
          t.role === "assistant"
            ? `room turn${t.provenance ? ` (${t.provenance})` : ""}`
            : `${t.provenance || "user"}: ${t.content.slice(0, 80)}`,
      })),
  ]
    .filter((e) => e.at)
    .sort((a, b) => parseTs(b.at).getTime() - parseTs(a.at).getTime())
    .slice(0, 40);

  return (
    <div className="p-4 flex flex-col gap-3 max-w-3xl">
      <Link to="/goals" className="text-[10px] text-accent hover:underline">← goals</Link>

      <header className="bg-surface border border-border p-3 flex flex-col gap-2">
        <div className="flex items-baseline gap-2">
          <span className={`text-[9px] uppercase ${goal.status === "active" ? "text-green-500" : "text-muted"}`}>
            {goal.status}
          </span>
          <span className="text-[10px] text-muted">{goal.kind}</span>
          {goal.deadline && <span className="text-[10px] text-muted">· due {fmtTs(goal.deadline)}</span>}
          {goal.version != null && <span className="text-[10px] text-muted">· v{goal.version}</span>}
        </div>
        <h1 className="text-sm text-text break-words">{goal.objective}</h1>
        <div className="text-[10px] text-muted flex flex-wrap gap-x-3">
          <span>
            room:{" "}
            <Link to="/conversations/$sessionKey" params={{ sessionKey: goal.conversation_id }} className="text-accent hover:underline break-all">
              {goal.conversation_id}
            </Link>
          </span>
          {goal.origin_conversation_id && (
            <span>origin: <span className="break-all">{goal.origin_conversation_id}</span></span>
          )}
          <span>created {fmtTs(goal.created_at)}</span>
        </div>

        {loop.on && (
          <div className="border border-border p-2 flex flex-col gap-1 text-[11px]">
            <div className="flex flex-wrap items-center gap-x-3">
              <span className="text-text">
                next:{" "}
                {loop.next_run
                  ? `${fmtTs(loop.next_run.at)} (${loop.next_run.frame})`
                  : branches.filter((b) => b.status === "pending").length > 0
                    ? `event — ${branches.filter((b) => b.status === "pending").length} branch(es) pending`
                    : "idle — dead-man"}
              </span>
              {loop.last_frame && <span className="text-muted">last frame: {loop.last_frame}</span>}
              {loop.stall_streak > 0 && <span className="text-amber-500">stall ×{loop.stall_streak}</span>}
            </div>
            {budgetPct != null && (
              <div className="flex items-center gap-2">
                <div className="flex-1 h-1.5 bg-border">
                  <div
                    className={`h-full ${budgetPct >= 100 ? "bg-red-500" : budgetPct > 70 ? "bg-amber-500" : "bg-green-600"}`}
                    style={{ width: `${budgetPct}%` }}
                  />
                </div>
                <span className="text-[10px] text-muted">
                  {loop.budget_spent}/{loop.budget_total} rounds
                </span>
              </div>
            )}
          </div>
        )}

        {goal.status === "active" && (
          <div className="flex gap-2">
            <button
              onClick={() => nudge.mutate()}
              disabled={nudge.isPending}
              className="text-[10px] uppercase border border-border px-2 py-0.5 text-accent hover:border-accent disabled:opacity-50"
            >
              nudge
            </button>
            <button
              onClick={() => pause.mutate()}
              disabled={pause.isPending || !loop.next_run}
              className="text-[10px] uppercase border border-border px-2 py-0.5 text-muted hover:border-accent disabled:opacity-50"
            >
              pause loop
            </button>
            <button
              onClick={() => confirm(`Cancel this goal?`) && cancel.mutate()}
              disabled={cancel.isPending}
              className="text-[10px] uppercase border border-red-900 px-2 py-0.5 text-red-400 disabled:opacity-50"
            >
              cancel goal
            </button>
          </div>
        )}
      </header>

      <Section title="state">
        {state.plan && <div className="text-xs text-text whitespace-pre-wrap">{state.plan}</div>}
        {state.strategies.length > 0 && (
          <div className="flex flex-col gap-1">
            {state.strategies.map((s) => (
              <div key={s.id} className="text-[11px] border border-border p-1.5">
                <span className="mr-1">{STRATEGY_MARK[s.status] ?? "?"}</span>
                <span className="text-text">{s.hypothesis}</span>
                <span className={`ml-1 text-[9px] uppercase ${s.status === "won" ? "text-green-500" : s.status === "pruned" ? "text-muted" : "text-amber-500"}`}>
                  {s.status}
                </span>
                {s.verdict && <div className="text-[10px] text-muted mt-0.5">{s.verdict}</div>}
              </div>
            ))}
          </div>
        )}
        {state.next_actions.length > 0 && (
          <div className="text-[11px]">
            {state.next_actions.map((na, i) => (
              <div key={i} className="text-text">
                · {na.action}
                {na.due && <span className="text-muted"> (due {fmtTs(na.due)})</span>}
              </div>
            ))}
          </div>
        )}
        {state.known.length > 0 && (
          <details>
            <summary className="text-[10px] text-muted cursor-pointer">
              evidence ({state.known.length})
            </summary>
            <div className="flex flex-col gap-0.5 mt-1">
              {state.known.map((k, i) => (
                <div key={i} className="text-[10px] text-muted break-words">· {k}</div>
              ))}
            </div>
          </details>
        )}
        {state.entities.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {state.entities.map((e) => (
              <span key={e} className="border border-border px-1 text-[10px] text-muted">{e}</span>
            ))}
          </div>
        )}
        {goal.result && (
          <div className="text-[11px] text-text border-t border-border pt-2">
            <span className="text-[9px] uppercase text-muted block mb-0.5">result</span>
            {goal.result}
          </div>
        )}
      </Section>

      <Section title={`branches (${branches.length})`}>
        {branches.length === 0 && <div className="text-[11px] text-muted">no tasks tied to this goal yet</div>}
        {branches.map((b) => (
          <div key={b.id} className="border border-border p-1.5 text-[11px] flex flex-col gap-0.5">
            <div className="flex items-baseline gap-2">
              <span className={`text-[9px] uppercase shrink-0 ${b.status === "pending" ? "text-amber-500" : b.status === "completed" ? "text-green-500" : "text-muted"}`}>
                {b.status}
              </span>
              <span className="text-text break-words">{b.title}</span>
            </div>
            <div className="text-[10px] text-muted">
              {b.id} · completer {b.completer || "open"} · due {fmtTs(b.due)}
            </div>
            {b.result && <div className="text-[10px] text-text break-words">{b.result}</div>}
          </div>
        ))}
      </Section>

      <Section title="timeline">
        {timeline.length === 0 && <div className="text-[11px] text-muted">nothing yet</div>}
        {timeline.map((e, i) => (
          <div key={i} className="text-[10px] flex gap-2">
            <span className="text-muted shrink-0 w-28">{fmtTs(e.at)}</span>
            <span className={`shrink-0 uppercase ${e.kind === "turn" ? "text-accent" : e.kind === "stimulus" ? "text-amber-500" : "text-muted"}`}>
              {e.kind}
            </span>
            <span className="text-text break-words min-w-0">{e.label}</span>
          </div>
        ))}
      </Section>
    </div>
  );
}

export const Route = createFileRoute("/goals/$goalId")({ component: GoalDetailPage });
