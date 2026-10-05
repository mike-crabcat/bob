import { createFileRoute, Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchAPI, postAPI } from "@/lib/api";
import { parseTs } from "@/lib/time";

// Work — everything owed, in one view (commitments plan Phase 6). Absorbed
// the old /goals list 2026-10-06 (loop status, cancel, settled history);
// goal detail stays at /goals/$goalId.

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
  open_promises: number;
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
  } else if (loop.open_promises > 0) {
    parts.push(`waiting on ${loop.open_promises} promise${loop.open_promises > 1 ? "s" : ""}`);
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


interface RunView {
  id: string;
  kind: string;
  summary: string;
  session_key: string;
  started_at: string;
  trace?: { call_id: string; session_key: string } | null;
}

interface Promise_ {
  id: string;
  text: string;
  completer: string | null;
  status: string;
}

interface GoalNode {
  id: string;
  objective: string;
  label: string;
  profile: string;
  parent_goal_id: string | null;
  conversation_id: string;
  origin_conversation_id: string | null;
  principal: string | null;
  deadline: string | null;
  next_wake: { at: string; kind: string } | null;
  promises: Promise_[];
  runs: RunView[];
}

interface WorkSnapshot {
  goals: GoalNode[];
  promises: { id: string; text: string; waiter: string; completer: string | null; due: string | null }[];
  suggestions: { id: string; text: string; session_key: string; created_at: string }[];
  background: RunView[];
}

function until(ts: string): string {
  const mins = Math.round((parseTs(ts).getTime() - Date.now()) / 60000);
  if (mins <= 0) return "now";
  if (mins < 90) return `in ${mins}m`;
  const h = Math.round(mins / 60);
  return h < 36 ? `in ${h}h` : `in ${Math.round(h / 24)}d`;
}

function ago(ts: string): string {
  const mins = Math.round((Date.now() - parseTs(ts).getTime()) / 60000);
  if (mins < 60) return `${mins}m`;
  const h = Math.round(mins / 60);
  return h < 36 ? `${h}h` : `${Math.round(h / 24)}d`;
}

function short(key: string | null | undefined): string {
  if (!key) return "—";
  return key.replace(/^agent:main:whatsapp:/, "").replace(/^agent:/, "");
}

function Section({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  return (
    <section>
      <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">
        {title} ({count})
      </h2>
      {count === 0 ? <div className="text-xs text-muted">none</div> : children}
    </section>
  );
}

function RunLine({ run }: { run: RunView }) {
  const body = (
    <span>
      <span className="text-accent">{run.kind}</span> {run.id.slice(0, 8)} · running {ago(run.started_at)} · {run.summary}
    </span>
  );
  return (
    <div className="text-[11px] text-muted flex items-center gap-1">
      <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
      {run.trace ? (
        <Link to="/conversations/$sessionKey/calls/$callId"
              params={{ sessionKey: run.trace.session_key, callId: run.trace.call_id }}
              className="hover:underline">{body}</Link>
      ) : body}
    </div>
  );
}

function GoalCard({ goal, kids, depth, loops, onCancel }: {
  goal: GoalNode; kids: Map<string, GoalNode[]>; depth: number;
  loops: Map<string, LoopSummary>; onCancel: (g: GoalNode) => void;
}) {
  const loop = loops.get(goal.id);
  const open = goal.promises.filter((p) => p.status === "pending");
  const done = goal.promises.length - open.length;
  return (
    <div className={`border border-border bg-surface p-2 flex flex-col gap-1 ${depth ? "ml-4" : ""}`}>
      <div className="flex items-center gap-2 text-[10px] text-muted flex-wrap">
        <span className="uppercase">{goal.label}</span>
        {goal.profile !== "outcome" && <span>· {goal.profile}</span>}
        {goal.principal && <span>· for {goal.principal}</span>}
        {goal.next_wake && !loop?.on && <span>· next {goal.next_wake.kind} {until(goal.next_wake.at)}</span>}
        {goal.deadline && <span>· deadline {until(goal.deadline)}</span>}
        {loop && <LoopLine loop={loop} />}
        <button onClick={() => onCancel(goal)}
                className="ml-auto text-[9px] uppercase text-red-400 border border-red-900 px-1 hover:bg-red-950">
          cancel
        </button>
      </div>
      <Link to="/goals/$goalId" params={{ goalId: goal.id }} className="text-xs text-text hover:underline">
        {goal.objective}
      </Link>
      {goal.promises.length > 0 && (
        <div className="text-[11px] text-muted">
          {open.length} open · {done} closed
          {open.slice(0, 8).map((p) => (
            <div key={p.id} className="pl-2">◦ {p.text}{p.completer ? ` → ${short(p.completer)}` : ""}</div>
          ))}
        </div>
      )}
      {goal.runs.map((r) => <RunLine key={r.id} run={r} />)}
      {(kids.get(goal.id) ?? []).map((k) => (
        <GoalCard key={k.id} goal={k} kids={kids} depth={depth + 1} loops={loops} onCancel={onCancel} />
      ))}
    </div>
  );
}

function WorkPage() {
  const { data } = useQuery<WorkSnapshot>({
    queryKey: ["work"],
    queryFn: () => fetchAPI<WorkSnapshot>("/work"),
    refetchInterval: 15000,
  });
  const { data: goalsSnap } = useQuery({
    queryKey: ["goals"],
    queryFn: () => fetchAPI<GoalsSnapshot>("/goals"),
    refetchInterval: 15000,
  });
  const qc = useQueryClient();
  const cancel = useMutation({
    mutationFn: (id: string) => postAPI(`/goals/${id}/cancel`, {}),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["work"] });
      qc.invalidateQueries({ queryKey: ["goals"] });
    },
  });
  if (!data) return <div className="p-4 text-muted text-center text-xs">loading...</div>;
  const onCancel = (g: GoalNode) => {
    if (confirm(`Cancel goal "${g.objective.slice(0, 60)}"?`)) cancel.mutate(g.id);
  };
  const loops = new Map((goalsSnap?.goals ?? []).map((g) => [g.id, g.loop] as const));
  const settled = (goalsSnap?.goals ?? []).filter((g) => g.status !== "active");

  const ids = new Set(data.goals.map((g) => g.id));
  const kids = new Map<string, GoalNode[]>();
  for (const g of data.goals) {
    if (g.parent_goal_id && ids.has(g.parent_goal_id)) {
      kids.set(g.parent_goal_id, [...(kids.get(g.parent_goal_id) ?? []), g]);
    }
  }
  const roots = data.goals.filter((g) => !g.parent_goal_id || !ids.has(g.parent_goal_id));

  return (
    <div className="flex flex-col gap-4 p-3">
      <Section title="Goals" count={roots.length}>
        <div className="flex flex-col gap-2">
          {roots.map((g) => <GoalCard key={g.id} goal={g} kids={kids} depth={0} loops={loops} onCancel={onCancel} />)}
        </div>
      </Section>

      <Section title="Promises" count={data.promises.length}>
        <div className="flex flex-col gap-px">
          {data.promises.map((p) => (
            <div key={p.id} className="bg-surface border border-border p-2 text-xs">
              <div className="text-text">{p.text}</div>
              <div className="text-[10px] text-muted">
                {p.id} · for {short(p.waiter)} · owed by {p.completer ? short(p.completer) : "itself"}
                {p.due && ` · due ${until(p.due)}`}
              </div>
            </div>
          ))}
        </div>
      </Section>

      <Section title="Suggested" count={data.suggestions.length}>
        <div className="flex flex-col gap-px">
          {data.suggestions.map((s) => (
            <div key={s.id} className="bg-surface border border-border p-2 text-xs">
              <div className="text-text">{s.text}</div>
              <div className="text-[10px] text-muted">offered in {short(s.session_key)} · {ago(s.created_at)} ago · awaiting their answer</div>
            </div>
          ))}
        </div>
      </Section>

      <Section title="Running in the background" count={data.background.length}>
        <div className="flex flex-col gap-1">
          {data.background.map((r) => <RunLine key={r.id} run={r} />)}
        </div>
      </Section>

      {settled.length > 0 && (
        <details className="flex flex-col gap-1">
          <summary className="text-xs text-muted font-sans uppercase tracking-wider cursor-pointer">
            Settled goals ({settled.length})
          </summary>
          <div className="flex flex-col gap-1 mt-1">
            {settled.map((g) => <GoalRow key={g.id} goal={g} />)}
          </div>
        </details>
      )}
    </div>
  );
}

export const Route = createFileRoute("/work")({ component: WorkPage });
