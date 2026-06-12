import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { fetchAPI, postAPI } from "@/lib/api";

interface DelegationDetail {
  id: string;
  session_key: string;
  user_story: string;
  plan: string | null;
  status: string;
  files_created: string[];
  result_summary: string | null;
  cost_usd: number;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

function StatusBadge({ status, large }: { status: string; large?: boolean }) {
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
    <span className={`px-2 py-1 ${large ? "text-xs" : "text-[10px]"} ${colors[status] ?? "bg-muted/20 text-muted"} ${active ? "animate-pulse" : ""}`}>
      {status.replace("_", " ")}
    </span>
  );
}

function RelativeTime({ iso }: { iso: string }) {
  if (!iso) return null;
  try {
    const d = new Date(iso);
    const diff = Date.now() - d.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return <span className="text-success">just now</span>;
    if (mins < 60) return <span>{mins}m ago</span>;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return <span>{hours}h ago</span>;
    return <span>{Math.floor(hours / 24)}d ago</span>;
  } catch {
    return null;
  }
}

function Collapsible({ title, defaultOpen = false, children }: {
  title: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section>
      <button onClick={() => setOpen(!open)} className="flex items-center gap-1 w-full text-left">
        <span className={`text-[10px] text-muted transition-transform ${open ? "rotate-90" : ""}`}>&#9654;</span>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider">{title}</h2>
      </button>
      {open && <div className="mt-1">{children}</div>}
    </section>
  );
}

function DelegationDetailPage() {
  const { delegationId } = Route.useParams();
  const queryClient = useQueryClient();

  const { data: d } = useQuery<DelegationDetail>({
    queryKey: ["skills-delegation", delegationId],
    queryFn: () => fetchAPI<DelegationDetail>(`/skills/delegations/${delegationId}`),
  });

  const implementMutation = useMutation({
    mutationFn: () => postAPI(`/skills/delegations/${delegationId}/implement`, {}),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills-delegation", delegationId] });
      queryClient.invalidateQueries({ queryKey: ["skills-delegations"] });
    },
  });

  const [rejectReason, setRejectReason] = useState("");
  const [showReject, setShowReject] = useState(false);
  const rejectMutation = useMutation({
    mutationFn: (reason: string) => postAPI(`/skills/delegations/${delegationId}/reject`, { reason }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skills-delegation", delegationId] });
      queryClient.invalidateQueries({ queryKey: ["skills-delegations"] });
    },
  });

  if (!d) {
    return <div className="p-4 text-muted text-center text-xs">loading...</div>;
  }

  const isActive = d.status === "planning" || d.status === "implementing";
  const isPlanReady = d.status === "plan_ready";

  return (
    <div className="flex flex-col gap-3 p-3">
      <div>
        <Link to="/skills" className="text-xs text-accent hover:underline">&larr; skills</Link>
      </div>

      <div className="flex items-center gap-2">
        <StatusBadge status={d.status} large />
        {isActive && (
          <span className="text-xs text-muted">
            {d.status === "planning" ? "Planning skill..." : "Implementing skill..."}
          </span>
        )}
      </div>

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">user story</h2>
        <div className="text-xs text-text bg-surface border border-border p-2 whitespace-pre-wrap">{d.user_story}</div>
      </section>

      {d.plan && (
        <Collapsible title="plan" defaultOpen={isPlanReady}>
          <div className="text-xs text-text bg-surface border border-border p-2 whitespace-pre-wrap max-h-[400px] overflow-y-auto">
            {d.plan}
          </div>
        </Collapsible>
      )}

      {d.result_summary && (
        <Collapsible title="result" defaultOpen={d.status === "completed"}>
          <div className="text-xs text-text bg-surface border border-border p-2 whitespace-pre-wrap max-h-[400px] overflow-y-auto">
            {d.result_summary}
          </div>
        </Collapsible>
      )}

      {d.error_message && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">error</h2>
          <div className="text-xs text-error bg-error/10 border border-error/20 p-2 whitespace-pre-wrap">{d.error_message}</div>
        </section>
      )}

      {d.files_created.length > 0 && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">files created</h2>
          <div className="flex flex-col gap-0.5">
            {d.files_created.map((f) => (
              <Link
                key={f}
                to="/workspace"
                className="text-xs text-accent hover:underline"
              >
                skills/{f}/
              </Link>
            ))}
          </div>
        </section>
      )}

      <div className="flex items-center gap-3 text-[10px] text-muted">
        <span>cost: ${d.cost_usd.toFixed(4)}</span>
        <span>created: <RelativeTime iso={d.created_at} /></span>
        <span>updated: <RelativeTime iso={d.updated_at} /></span>
      </div>

      {isPlanReady && (
        <section className="flex flex-col gap-2">
          <button
            onClick={() => implementMutation.mutate()}
            disabled={implementMutation.isPending}
            className="text-[10px] bg-accent text-bg px-3 py-1.5 hover:opacity-90 disabled:opacity-50"
          >
            {implementMutation.isPending ? "implementing..." : "implement plan"}
          </button>

          {!showReject ? (
            <button
              onClick={() => setShowReject(true)}
              className="text-[10px] text-muted hover:text-error px-3 py-1.5"
            >
              reject
            </button>
          ) : (
            <div className="flex flex-col gap-1">
              <input
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                placeholder="reason..."
                className="bg-surface border border-border text-xs text-text px-2 py-1"
              />
              <div className="flex items-center gap-2">
                <button
                  onClick={() => rejectMutation.mutate(rejectReason)}
                  disabled={rejectMutation.isPending || !rejectReason.trim()}
                  className="text-[10px] bg-error/20 text-error px-3 py-1 hover:opacity-90 disabled:opacity-50"
                >
                  {rejectMutation.isPending ? "rejecting..." : "confirm reject"}
                </button>
                <button
                  onClick={() => setShowReject(false)}
                  className="text-[10px] text-muted hover:text-text px-3 py-1"
                >
                  cancel
                </button>
              </div>
            </div>
          )}

          {implementMutation.isError && (
            <div className="text-[10px] text-error">implementation failed</div>
          )}
          {rejectMutation.isError && (
            <div className="text-[10px] text-error">rejection failed</div>
          )}
        </section>
      )}
    </div>
  );
}

export const Route = createFileRoute("/skills/$delegationId")({ component: DelegationDetailPage });
