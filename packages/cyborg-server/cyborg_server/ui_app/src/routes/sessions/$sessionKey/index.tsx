import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchAPI } from "@/lib/api";

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

function SessionDetailPage() {
  const { sessionKey } = Route.useParams();

  const { data: detail } = useQuery<SessionDetail>({
    queryKey: ["session-detail", sessionKey],
    queryFn: () => fetchAPI<SessionDetail>(`/sessions/${encodeURIComponent(sessionKey)}`),
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

      {detail.current_agenda && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">agenda</h2>
          <div className="text-xs text-text bg-surface border border-border p-2 whitespace-pre-wrap">
            {detail.current_agenda}
          </div>
        </section>
      )}

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">
          timeline ({detail.calls.length} calls, {detail.summaries.length} summaries)
        </h2>
        {buildTimeline(detail).map((entry) =>
          entry.kind === "summary" ? (
            <div key={`s-${entry.data.id}`} className="bg-accent/10 border-l-2 border-accent p-2 mb-px">
              <div className="text-[10px] text-accent mb-0.5">summary · {entry.data.message_count} msgs</div>
              <div className="text-xs text-text">{entry.data.summary_text}</div>
              {entry.data.topics.length > 0 && (
                <div className="text-[10px] text-muted mt-1">{entry.data.topics.join(", ")}</div>
              )}
            </div>
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
