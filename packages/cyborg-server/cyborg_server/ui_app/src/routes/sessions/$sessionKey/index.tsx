import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type React from "react";
import { useState } from "react";
import { fetchAPI, postAPI, putAPI } from "@/lib/api";
import { RichText } from "@/components/shared/rich-text";

interface SessionDetail {
  session_key: string;
  channel: string;
  calls: CallItem[];
  participants: ParticipantItem[];
  summaries: SummaryItem[];
  current_agenda: string;
  stats: { total_calls: number; completed: number; failed: number };
}

interface CallItem {
  id: string;
  created_at: string;
  call_category: string;
  status: string;
  latency_seconds: number | null;
  model: string;
  user_message: string;
  response_preview: string;
  error_message: string | null;
  contact_id: string | null;
  contact_name: string | null;
}

interface ParticipantItem {
  display_name: string;
  identifier: string;
  contact_id: string | null;
  is_trusted: boolean;
  last_active: string;
}

interface SummaryItem {
  id: string;
  active_from: string;
  active_to: string;
  summary_text: string;
  topics: string[];
  memory_prompts: string[];
  message_count: number;
}

type TimelineEntry =
  | { kind: "call"; data: CallItem }
  | { kind: "summary"; data: SummaryItem };

function toMs(ts: string): number {
  if (!ts) return 0;
  return new Date(ts.endsWith("Z") ? ts : ts + "Z").getTime() || 0;
}

function stripMetadataEnvelope(text: string): string {
  return text.replace(/^#{2,} .*\n(?:(?!#{2,} ).*\S.*\n)*\n/, "").trimStart();
}

function buildTimeline(detail: SessionDetail): TimelineEntry[] {
  const items: (TimelineEntry & { time: number })[] = [
    ...detail.calls.map((c) => ({ kind: "call" as const, time: toMs(c.created_at), data: c })),
    ...detail.summaries.map((s) => ({ kind: "summary" as const, time: toMs(s.active_to), data: s })),
  ];
  return items.sort((a, b) => b.time - a.time);
}

function ReflectionCard({ call }: { call: CallItem }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="bg-accent/5 border-l-2 border-accent/40 p-2 mb-px">
      <div className="text-[10px] text-accent/70 mb-0.5">reflection</div>
      <div className={`text-xs text-text whitespace-pre-wrap ${expanded ? "" : "line-clamp-6"}`}>
        <span className="font-medium text-muted">Q: </span>
        {call.user_message}
      </div>
      {call.response_preview && (
        <div className={`text-xs text-text mt-1 whitespace-pre-wrap ${expanded ? "" : "line-clamp-6"}`}>
          <span className="font-medium text-accent">A: </span>
          {call.response_preview}
        </div>
      )}
      {call.error_message && (
        <div className="text-xs text-error mt-0.5">{call.error_message}</div>
      )}
      <button
        onClick={() => setExpanded(!expanded)}
        className="text-[10px] text-accent/70 hover:text-accent mt-1"
      >
        {expanded ? "collapse" : "expand"}
      </button>
    </div>
  );
}

function SessionDetailPage() {
  const { sessionKey } = Route.useParams();
  const queryClient = useQueryClient();
  const [reflectOpen, setReflectOpen] = useState(false);
  const [reflectQuery, setReflectQuery] = useState("");
  const [editingAgenda, setEditingAgenda] = useState(false);
  const [editAgenda, setEditAgenda] = useState("");

  const { data: detail } = useQuery<SessionDetail>({
    queryKey: ["session-detail", sessionKey],
    queryFn: () => fetchAPI<SessionDetail>(`/sessions/${encodeURIComponent(sessionKey)}`),
  });

  const reflectMutation = useMutation({
    mutationFn: (query: string) =>
      postAPI<{ response_text: string }>(
        `/sessions/${encodeURIComponent(sessionKey)}/reflect`,
        { query },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["session-detail", sessionKey] });
      setReflectOpen(false);
      setReflectQuery("");
    },
  });

  const agendaMutation = useMutation({
    mutationFn: (agenda: string) =>
      putAPI<{ ok: boolean }>(
        `/sessions/${encodeURIComponent(sessionKey)}/agenda`,
        { agenda },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["session-detail", sessionKey] });
      setEditingAgenda(false);
    },
  });

  if (!detail) {
    return <div className="p-4 text-muted text-center text-xs">loading...</div>;
  }

  return (
    <div className="flex flex-col gap-3 p-3">
      <div>
        <Link to="/sessions" className="text-xs text-accent hover:underline">&larr; sessions</Link>
        <h1 className="text-sm font-medium mt-1 break-all">{detail.session_key}</h1>
        <div className="flex items-center gap-2 mt-1 text-[10px] text-muted">
          <span className="uppercase">{detail.channel}</span>
          <span>{detail.stats.total_calls} calls</span>
          <span className="text-success">{detail.stats.completed} ok</span>
          {detail.stats.failed > 0 && <span className="text-error">{detail.stats.failed} failed</span>}
        </div>
      </div>

      {detail.participants.length > 0 && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">participants</h2>
          {detail.participants.map((p) => (
            <div key={p.identifier} className="flex items-center gap-1 text-xs py-0.5">
              <span className={`w-1.5 h-1.5 rounded-full ${p.is_trusted ? "bg-success" : "bg-muted"}`} />
              <span className="text-text">{p.display_name}</span>
              {p.is_trusted && <span className="text-[9px] text-success">trusted</span>}
            </div>
          ))}
        </section>
      )}

      <section>
        <div className="flex items-center gap-2 mb-1">
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider">agenda</h2>
          {!editingAgenda && (
            <button
              onClick={() => { setEditAgenda(detail.current_agenda ?? ""); setEditingAgenda(true); }}
              className="text-[10px] text-accent hover:underline"
            >
              edit
            </button>
          )}
        </div>
        {editingAgenda ? (
          <div className="flex flex-col gap-1">
            <textarea
              value={editAgenda}
              onChange={(e) => setEditAgenda(e.target.value)}
              className="bg-surface border border-border text-xs text-text px-2 py-1 min-h-[80px] resize-y font-mono"
              rows={6}
            />
            <div className="flex items-center gap-2">
              <button
                onClick={() => agendaMutation.mutate(editAgenda)}
                disabled={agendaMutation.isPending}
                className="text-[10px] bg-accent text-bg px-3 py-1 hover:opacity-90 disabled:opacity-50"
              >
                {agendaMutation.isPending ? "saving..." : "save"}
              </button>
              <button
                onClick={() => setEditingAgenda(false)}
                disabled={agendaMutation.isPending}
                className="text-[10px] text-muted hover:text-text px-3 py-1"
              >
                cancel
              </button>
              {agendaMutation.isError && (
                <span className="text-[10px] text-error">save failed</span>
              )}
            </div>
          </div>
        ) : detail.current_agenda ? (
          <div className="text-xs text-text bg-surface border border-border p-2 whitespace-pre-wrap">
            {detail.current_agenda}
          </div>
        ) : (
          <div className="text-xs text-muted">no agenda set</div>
        )}
      </section>

      <section>
        <button
          onClick={() => setReflectOpen(!reflectOpen)}
          className="text-xs text-accent hover:underline"
        >
          reflect...
        </button>
        {reflectOpen && (
          <div className="flex flex-col gap-1 mt-1">
            <textarea
              value={reflectQuery}
              onChange={(e) => setReflectQuery(e.target.value)}
              placeholder="Why did you not post the image?"
              className="bg-surface border border-border text-xs text-text px-2 py-1 min-h-[60px] resize-y"
              rows={2}
            />
            <div className="flex items-center gap-2">
              <button
                onClick={() => reflectMutation.mutate(reflectQuery)}
                disabled={!reflectQuery.trim() || reflectMutation.isPending}
                className="text-[10px] bg-accent text-bg px-3 py-1 hover:opacity-90 disabled:opacity-50"
              >
                {reflectMutation.isPending ? "analyzing..." : "submit"}
              </button>
              <button
                onClick={() => { setReflectOpen(false); setReflectQuery(""); }}
                className="text-[10px] text-muted hover:text-text px-3 py-1"
              >
                cancel
              </button>
              {reflectMutation.isError && (
                <span className="text-[10px] text-error">reflection failed</span>
              )}
            </div>
          </div>
        )}
      </section>

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">
          timeline ({detail.calls.length} calls, {detail.summaries.length} summaries)
        </h2>
        {buildTimeline(detail).map((entry) =>
          entry.kind === "summary" ? (
            <div key={`s-${entry.data.id}`} className="bg-accent/10 border-l-2 border-accent p-2 mb-px">
              <div className="text-[10px] text-accent mb-0.5">summary · {entry.data.message_count} msgs</div>
              <div className="text-xs text-text"><RichText text={entry.data.summary_text} /></div>
              {entry.data.topics.length > 0 && (
                <div className="text-[10px] text-muted mt-1">{entry.data.topics.map((t, i) => <RichText key={i} text={t} />).reduce<React.ReactNode[]>((acc, el, i) => i === 0 ? [el] : [...acc, ", ", el], [])}</div>
              )}
            </div>
          ) : entry.data.call_category === "reflection" ? (
            <ReflectionCard key={`c-${entry.data.id}`} call={entry.data} />
          ) : (
            <Link
              key={`c-${entry.data.id}`}
              to="/sessions/$sessionKey/calls/$callId"
              params={{ sessionKey, callId: entry.data.id }}
              className="block border-b border-border py-2 hover:bg-surface transition-colors"
            >
              <div className="flex items-center gap-2 text-[10px] text-muted">
                <span>{entry.data.call_category}</span>
                <span>{entry.data.model}</span>
                <span className={entry.data.status === "completed" ? "text-success" : "text-error"}>
                  {entry.data.status}
                </span>
                {entry.data.latency_seconds != null && <span>{entry.data.latency_seconds.toFixed(2)}s</span>}
                <span className="ml-auto text-muted text-xs">&rsaquo;</span>
              </div>
              {entry.data.user_message && (
                <div className="text-xs text-text mt-1 whitespace-pre-wrap line-clamp-4">
                  {entry.data.contact_id ? (
                    <Link
                      to="/contacts/$contactId"
                      params={{ contactId: entry.data.contact_id }}
                      onClick={(e) => e.stopPropagation()}
                      className="text-accent font-medium hover:underline"
                    >
                      {entry.data.contact_name || "unknown"}
                    </Link>
                  ) : (
                    <span className="font-medium">{entry.data.contact_name || "unknown"}</span>
                  )}
                  <span>: {stripMetadataEnvelope(entry.data.user_message)}</span>
                </div>
              )}
              {entry.data.response_preview && (
                <div className="text-xs text-muted mt-0.5 whitespace-pre-wrap line-clamp-4">
                  <span className="text-accent">cyborg:</span> {entry.data.response_preview}
                </div>
              )}
              {entry.data.error_message && (
                <div className="text-xs text-error mt-0.5">{entry.data.error_message}</div>
              )}
            </Link>
          ),
        )}
      </section>
    </div>
  );
}

export const Route = createFileRoute("/sessions/$sessionKey/")({ component: SessionDetailPage });
