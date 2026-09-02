import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchAPI, postAPI } from "@/lib/api";

export interface OpsStatus {
  quota_gate: { open: boolean; trip_count: number; remaining_s: number };
  effects: {
    counts: Record<string, number>;
    pending: number;
    dead: number;
    dead_effects: DeadEffect[];
  };
  goals: { active: number; overdue: OverdueGoal[] };
  wakeups: { scheduled: number; next: { not_before: string; kind: string } | null };
  stuck_turns: StuckTurn[];
  undispatched_48h: number;
  db_bytes: number;
}

export interface DeadEffect {
  id: string;
  kind: string;
  attempt: number;
  error: string | null;
  payload_preview: string;
  created_at: string;
}

export interface OverdueGoal {
  id: string;
  objective: string;
  kind: string;
  deadline: string;
  conversation_id: string;
}

export interface StuckTurn {
  id: string;
  conversation_id: string;
  attempt: number;
  started_at: string;
  lease_expires_at: string;
}

export function useOpsStatus() {
  return useQuery<OpsStatus>({
    queryKey: ["ops-status"],
    queryFn: () => fetchAPI<OpsStatus>("/status"),
    refetchInterval: 30_000,
  });
}

function fmtBytes(n: number): string {
  if (n >= 1_073_741_824) return (n / 1_073_741_824).toFixed(1) + " GB";
  if (n >= 1_048_576) return (n / 1_048_576).toFixed(0) + " MB";
  return (n / 1024).toFixed(0) + " KB";
}

function Pill({
  label,
  value,
  tone = "ok",
}: {
  label: string;
  value: string | number;
  tone?: "ok" | "warn" | "bad";
}) {
  const toneClass =
    tone === "bad"
      ? "border-red-500/60 text-red-400"
      : tone === "warn"
        ? "border-yellow-500/60 text-yellow-400"
        : "border-border text-text";
  return (
    <div className={`bg-surface border px-2 py-1 ${toneClass}`}>
      <span className="text-[10px] font-sans uppercase text-muted mr-1.5">{label}</span>
      <span className="text-xs tabular-nums font-medium">{value}</span>
    </div>
  );
}

export function HealthStrip() {
  const { data: s } = useOpsStatus();
  if (!s) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      <Pill
        label="quota"
        value={s.quota_gate.open ? `OPEN ${s.quota_gate.remaining_s}s` : "ok"}
        tone={s.quota_gate.open ? "bad" : "ok"}
      />
      <Pill
        label="effects"
        value={s.effects.dead > 0 ? `${s.effects.dead} dead` : `${s.effects.pending} pending`}
        tone={s.effects.dead > 0 ? "bad" : "ok"}
      />
      <Pill label="goals" value={s.goals.active} tone={s.goals.overdue.length > 0 ? "warn" : "ok"} />
      <Pill label="wakeups" value={s.wakeups.scheduled} />
      <Pill
        label="undispatched"
        value={s.undispatched_48h}
        tone={s.undispatched_48h > 0 ? "warn" : "ok"}
      />
      <Pill label="db" value={fmtBytes(s.db_bytes)} />
    </div>
  );
}

export function NeedsAttention() {
  const { data: s } = useOpsStatus();
  const qc = useQueryClient();
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "retry" | "discard" }) =>
      postAPI(`/effects/${id}/${action}`, {}),
    onSettled: () => qc.invalidateQueries({ queryKey: ["ops-status"] }),
  });
  const retryTurn = useMutation({
    mutationFn: (id: string) => postAPI(`/turns/${id}/retry`, {}),
    onSettled: () => qc.invalidateQueries({ queryKey: ["ops-status"] }),
  });

  if (!s) return null;
  const items =
    s.effects.dead_effects.length + s.goals.overdue.length + s.stuck_turns.length;
  if (items === 0) return null;

  return (
    <section>
      <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">
        needs attention <span className="text-red-400 ml-1">{items}</span>
      </h2>
      <div className="bg-surface border border-border divide-y divide-border text-xs">
        {s.effects.dead_effects.map((e) => (
          <div key={e.id} className="p-2 flex items-start gap-2">
            <span className="text-red-400 shrink-0">dead effect</span>
            <div className="min-w-0 flex-1">
              <div className="text-text">
                {e.kind} · attempt {e.attempt}
              </div>
              <div className="text-muted truncate">{e.error}</div>
            </div>
            <button
              className="border border-border px-2 py-0.5 hover:bg-border shrink-0"
              disabled={act.isPending}
              onClick={() => act.mutate({ id: e.id, action: "retry" })}
            >
              retry
            </button>
            <button
              className="border border-border px-2 py-0.5 text-muted hover:bg-border shrink-0"
              disabled={act.isPending}
              onClick={() => act.mutate({ id: e.id, action: "discard" })}
            >
              discard
            </button>
          </div>
        ))}
        {s.goals.overdue.map((g) => (
          <div key={g.id} className="p-2 flex items-start gap-2">
            <span className="text-yellow-400 shrink-0">overdue goal</span>
            <div className="min-w-0 flex-1">
              <div className="text-text truncate">{g.objective}</div>
              <div className="text-muted">
                {g.kind} · due {new Date(g.deadline).toLocaleString()}
              </div>
            </div>
          </div>
        ))}
        {s.stuck_turns.map((t) => (
          <div key={t.id} className="p-2 flex items-start gap-2">
            <span className="text-yellow-400 shrink-0">stuck turn</span>
            <div className="min-w-0 flex-1">
              <div className="text-text truncate">{t.conversation_id}</div>
              <div className="text-muted">
                attempt {t.attempt} · lease expired{" "}
                {new Date(t.lease_expires_at).toLocaleString()}
              </div>
            </div>
            <button
              className="border border-border px-2 py-0.5 hover:bg-border shrink-0"
              disabled={retryTurn.isPending}
              onClick={() => retryTurn.mutate(t.id)}
            >
              retry now
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
