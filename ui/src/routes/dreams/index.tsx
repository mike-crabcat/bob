import { createFileRoute } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { fetchAPI, postAPI } from "@/lib/api";

interface DreamRun {
  id: string;
  started_at: string;
  finished_at: string | null;
  window_start: string;
  window_end: string;
  status: string;
  trigger: string;
  model: string;
  sessions_reviewed_json: string;
  stats_json: string;
  journal_text: string;
  error: string | null;
}

interface DreamPlan {
  id: string;
  title: string;
  what_was_discussed: string;
  proposed_action: string;
  assistance_method: string;
  status: string;
  approved_by: string | null;
  approved_at: string | null;
  announced_at: string | null;
  reannounced_at: string | null;
  due_hint: string | null;
  created_at: string;
  updated_at: string;
}

interface DreamResolution {
  id: string;
  title: string;
  behaviour: string;
  trigger_condition: string;
  success_signal: string;
  status: string;
  first_seen_at: string;
  last_seen_at: string;
  observation_count: number;
}

interface DreamStats {
  enabled: boolean;
  draft_mode: boolean;
  autoplan_sessions: string[];
  interval_minutes: number;
  plans_by_status: Record<string, number>;
  resolutions_by_status: Record<string, number>;
  last_run: DreamRun | null;
}

interface Announcement {
  session_key: string;
  content: string;
  created_at: string;
}

const TABS = ["journal", "resolutions", "plans", "controls"] as const;
type Tab = (typeof TABS)[number];

const PLAN_STATUSES = ["draft", "proposed", "approved", "actioned", "completed", "expired", "dismissed"];
const RESOLUTION_STATUSES = ["draft", "open", "in_program", "kept", "dropped", "stale"];

function StatusChip({ status }: { status: string }) {
  const colors: Record<string, string> = {
    draft: "bg-muted/20 text-muted",
    proposed: "bg-accent/20 text-accent",
    approved: "bg-success/20 text-success",
    actioned: "bg-success/20 text-success",
    completed: "bg-success/20 text-success",
    kept: "bg-success/20 text-success",
    open: "bg-accent/20 text-accent",
    in_program: "bg-accent/20 text-accent",
    expired: "bg-muted/20 text-muted",
    dismissed: "bg-muted/20 text-muted",
    dropped: "bg-muted/20 text-muted",
    stale: "bg-muted/20 text-muted",
    running: "bg-accent/20 text-accent animate-pulse",
    complete: "bg-success/20 text-success",
    failed: "bg-error/20 text-error",
  };
  return (
    <span className={`text-[10px] px-1.5 py-0.5 shrink-0 ${colors[status] ?? "bg-muted/20 text-muted"}`}>
      {status.replace("_", " ")}
    </span>
  );
}

function JournalTab() {
  const { data } = useQuery<{ runs: DreamRun[] }>({
    queryKey: ["dream-runs"],
    queryFn: () => fetchAPI<{ runs: DreamRun[] }>("/dreams/runs"),
    refetchInterval: 30000,
  });
  const runs = data?.runs ?? [];
  const [openId, setOpenId] = useState<string | null>(null);

  if (runs.length === 0) {
    return <div className="px-3 py-4 text-[10px] text-muted">no dream runs yet</div>;
  }
  return (
    <div className="overflow-y-auto">
      {runs.map((run) => {
        const open = openId === run.id;
        let stats: Record<string, unknown> = {};
        try { stats = JSON.parse(run.stats_json || "{}"); } catch { /* empty */ }
        const sessions = Array.isArray(stats.sessions) ? (stats.sessions as unknown[]).length : 0;
        const created = Array.isArray(stats.plans_created) ? (stats.plans_created as unknown[]).length : 0;
        const resolutions = Array.isArray(stats.resolutions_created) ? (stats.resolutions_created as unknown[]).length : 0;
        return (
          <div key={run.id} className="border-b border-border">
            <button
              onClick={() => setOpenId(open ? null : run.id)}
              className="w-full text-left px-3 py-2 hover:bg-muted/5 flex items-center gap-2"
            >
              <StatusChip status={run.status} />
              <span className="text-xs text-text truncate">{run.id}</span>
              <span className="text-[10px] text-muted ml-auto shrink-0">
                {run.trigger} · {sessions} sess · {created} plan · {resolutions} res
              </span>
            </button>
            {open && (
              <div className="px-3 pb-3 text-[10px] text-muted space-y-2">
                <div>
                  window {run.window_start?.slice(0, 16)} → {run.window_end?.slice(0, 16)} · model {run.model || "?"}
                </div>
                {run.error && <div className="text-error whitespace-pre-wrap">{run.error}</div>}
                {run.journal_text && (
                  <pre className="whitespace-pre-wrap font-sans text-[11px] text-text bg-muted/5 p-2 border border-border">
                    {run.journal_text}
                  </pre>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function ResolutionsTab() {
  const [filter, setFilter] = useState("all");
  const queryClient = useQueryClient();
  const { data } = useQuery<{ resolutions: DreamResolution[] }>({
    queryKey: ["dream-resolutions"],
    queryFn: () => fetchAPI<{ resolutions: DreamResolution[] }>("/dreams/resolutions"),
    refetchInterval: 30000,
  });
  const resolutions = data?.resolutions ?? [];
  const filtered = filter === "all" ? resolutions : resolutions.filter((r) => r.status === filter);

  const act = async (id: string, action: "promote" | "drop") => {
    await postAPI(`/dreams/resolutions/${id}/${action}`, {});
    await queryClient.invalidateQueries({ queryKey: ["dream-resolutions"] });
  };

  return (
    <div className="flex flex-col h-full">
      <div className="flex gap-1 px-3 py-1.5 border-b border-border overflow-x-auto shrink-0">
        {["all", ...RESOLUTION_STATUSES].map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-2 py-1 text-[11px] border border-border shrink-0 transition-colors ${
              filter === f ? "bg-accent text-bg" : "text-muted hover:text-text"
            }`}
          >
            {f.replace("_", " ")}
          </button>
        ))}
      </div>
      <div className="overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="px-3 py-4 text-[10px] text-muted">no resolutions</div>
        ) : (
          filtered.map((r) => (
            <div key={r.id} className="px-3 py-2 border-b border-border">
              <div className="flex items-center gap-2">
                <StatusChip status={r.status} />
                <span className="text-xs font-medium text-text truncate">{r.title}</span>
                <span className="text-[10px] text-muted ml-auto shrink-0">×{r.observation_count}</span>
              </div>
              <div className="text-[10px] text-muted mt-0.5">{r.behaviour}</div>
              <div className="text-[10px] text-muted/60 mt-0.5">when: {r.trigger_condition}</div>
              <div className="text-[10px] text-muted/60 mt-0.5">success: {r.success_signal}</div>
              {r.status === "draft" && (
                <div className="flex gap-1 mt-1">
                  <button onClick={() => act(r.id, "promote")} className="px-2 py-0.5 text-[10px] border border-border text-success hover:bg-success/10">promote</button>
                  <button onClick={() => act(r.id, "drop")} className="px-2 py-0.5 text-[10px] border border-border text-muted hover:bg-muted/10">drop</button>
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function PlansTab() {
  const [filter, setFilter] = useState("all");
  const queryClient = useQueryClient();
  const { data } = useQuery<{ plans: DreamPlan[] }>({
    queryKey: ["dream-plans"],
    queryFn: () => fetchAPI<{ plans: DreamPlan[] }>("/dreams/plans"),
    refetchInterval: 30000,
  });
  const plans = data?.plans ?? [];
  const drafts = plans.filter((p) => p.status === "draft");
  const filtered = filter === "all" ? plans : plans.filter((p) => p.status === filter);

  const act = async (id: string, action: "approve" | "dismiss") => {
    await postAPI(`/dreams/plans/${id}/${action}`, {});
    await queryClient.invalidateQueries({ queryKey: ["dream-plans"] });
  };

  return (
    <div className="flex flex-col h-full">
      {drafts.length > 0 && (
        <div className="px-3 py-1.5 border-b border-border bg-accent/5 shrink-0">
          <span className="text-[10px] text-accent uppercase tracking-wider">draft review queue — {drafts.length}</span>
        </div>
      )}
      <div className="flex gap-1 px-3 py-1.5 border-b border-border overflow-x-auto shrink-0">
        {["all", ...PLAN_STATUSES].map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-2 py-1 text-[11px] border border-border shrink-0 transition-colors ${
              filter === f ? "bg-accent text-bg" : "text-muted hover:text-text"
            }`}
          >
            {f}
          </button>
        ))}
      </div>
      <div className="overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="px-3 py-4 text-[10px] text-muted">no plans</div>
        ) : (
          filtered.map((p) => (
            <div key={p.id} className="px-3 py-2 border-b border-border">
              <div className="flex items-center gap-2">
                <StatusChip status={p.status} />
                <span className="text-xs font-medium text-text truncate">{p.title}</span>
                {p.due_hint && <span className="text-[10px] text-muted shrink-0">due: {p.due_hint}</span>}
              </div>
              <div className="text-[10px] text-muted mt-0.5">{p.what_was_discussed}</div>
              <div className="text-[10px] text-muted/60 mt-0.5">next: {p.proposed_action}</div>
              <div className="text-[10px] text-muted/60 mt-0.5">help: {p.assistance_method}</div>
              <div className="text-[10px] text-muted/40 mt-0.5">
                {p.id}
                {p.approved_by && ` · approved by ${p.approved_by}`}
                {p.announced_at && ` · announced ${p.announced_at.slice(0, 10)}`}
                {p.reannounced_at && " (+follow-up)"}
              </div>
              {(p.status === "draft" || p.status === "proposed") && (
                <div className="flex gap-1 mt-1">
                  <button onClick={() => act(p.id, "approve")} className="px-2 py-0.5 text-[10px] border border-border text-success hover:bg-success/10">approve</button>
                  <button onClick={() => act(p.id, "dismiss")} className="px-2 py-0.5 text-[10px] border border-border text-muted hover:bg-muted/10">dismiss</button>
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function ControlsTab() {
  const queryClient = useQueryClient();
  const { data: stats } = useQuery<DreamStats>({
    queryKey: ["dream-stats"],
    queryFn: () => fetchAPI<DreamStats>("/dreams/stats"),
  });
  const { data: ann } = useQuery<{ announcements: Announcement[] }>({
    queryKey: ["dream-announcements"],
    queryFn: () => fetchAPI<{ announcements: Announcement[] }>("/dreams/announcements"),
  });

  const toggleAutoplan = async (sessionKey: string, enabled: boolean) => {
    await postAPI("/dreams/autoplan", { session_key: sessionKey, enabled });
    await queryClient.invalidateQueries({ queryKey: ["dream-stats"] });
  };
  const runNow = async () => {
    await postAPI("/dreams/run", {});
    await queryClient.invalidateQueries({ queryKey: ["dream-runs"] });
  };

  return (
    <div className="overflow-y-auto text-[11px]">
      <div className="px-3 py-2 border-b border-border space-y-1">
        <div className="text-[10px] uppercase tracking-wider text-muted">autoplan (per chat — set with /autoplan in WhatsApp)</div>
        {(stats?.autoplan_sessions ?? []).length === 0 ? (
          <div className="text-[10px] text-muted">off everywhere — plans await manual approval</div>
        ) : (
          (stats?.autoplan_sessions ?? []).map((sk) => (
            <div key={sk} className="flex items-center gap-2">
              <span className="text-success">ON</span>
              <span className="text-muted truncate">{sk}</span>
              <button
                onClick={() => toggleAutoplan(sk, false)}
                className="ml-auto px-2 py-0.5 border border-border text-muted hover:bg-muted/10 shrink-0"
              >
                turn off
              </button>
            </div>
          ))
        )}
        <div className="flex items-center gap-2">
          <span className="text-muted">dream</span>
          <button onClick={runNow} className="px-2 py-0.5 border border-border text-accent hover:bg-accent/10">run now</button>
        </div>
        <div className="text-[10px] text-muted/60">
          enabled={String(stats?.enabled ?? false)} · draft_mode={String(stats?.draft_mode ?? true)} ·
          interval={stats?.interval_minutes ?? "?"}m
        </div>
      </div>
      <div className="px-3 py-2 border-b border-border">
        <h3 className="text-[10px] uppercase tracking-wider text-muted mb-1">recent announcements</h3>
        {(ann?.announcements ?? []).length === 0 ? (
          <div className="text-[10px] text-muted">none yet</div>
        ) : (
          (ann?.announcements ?? []).map((a, i) => (
            <div key={i} className="py-1 border-b border-border/50 last:border-0">
              <div className="text-[10px] text-muted/50 truncate">{a.session_key} · {a.created_at?.slice(0, 16)}</div>
              <div className="text-text">{a.content}</div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function DreamsPage() {
  const [tab, setTab] = useState<Tab>("journal");

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-border">
        <h1 className="text-xs font-sans uppercase tracking-wider text-muted">dreams</h1>
      </div>
      <div className="flex gap-1 px-3 py-1.5 border-b border-border shrink-0">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-2 py-1 text-[11px] border border-border transition-colors ${
              tab === t ? "bg-accent text-bg" : "text-muted hover:text-text"
            }`}
          >
            {t}
          </button>
        ))}
      </div>
      <div className="flex-1 min-h-0">
        {tab === "journal" && <JournalTab />}
        {tab === "resolutions" && <ResolutionsTab />}
        {tab === "plans" && <PlansTab />}
        {tab === "controls" && <ControlsTab />}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/dreams/")({
  component: DreamsPage,
});
