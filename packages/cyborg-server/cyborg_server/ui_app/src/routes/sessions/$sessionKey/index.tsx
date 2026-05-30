import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type React from "react";
import { useMemo, useRef, useState } from "react";
import { fetchAPI, postAPI, putAPI } from "@/lib/api";
import { RichText } from "@/components/shared/rich-text";
import { useWSEvents } from "@/hooks/use-live-data";

interface SessionContext {
  kind: "group" | "dm" | "thread" | null;
  display_name: string | null;
  description: string | null;
  member_count: number | null;
  email_participants: { email: string; name: string | null }[] | null;
}

interface SessionDetail {
  session_key: string;
  channel: string;
  session_context: SessionContext;
  calls: CallItem[];
  messages: MessageItem[];
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
  ttft_seconds: number | null;
  total_tokens: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  tool_count: number;
  model: string;
  user_message: string;
  response_preview: string;
  error_message: string | null;
  contact_id: string | null;
  contact_name: string | null;
  tools?: string[];
}

interface ParticipantItem {
  display_name: string;
  identifier: string;
  contact_id: string | null;
  is_trusted: boolean;
  last_active: string;
}

interface MessageItem {
  id: string;
  role: string;
  content: string;
  channel: string;
  sender_id: string | null;
  sender_name: string | null;
  created_at: string;
}

interface SummaryItem {
  id: string;
  active_from: string;
  active_to: string;
  summary_text: string;
  topics: string[];
  memory_prompts: string[];
  message_count: number;
  created_at: string;
}

type TimelineEntry =
  | { kind: "call"; data: CallItem }
  | { kind: "message"; data: MessageItem }
  | { kind: "summary"; data: SummaryItem };

function toMs(ts: string): number {
  if (!ts) return 0;
  return new Date(ts.endsWith("Z") ? ts : ts + "Z").getTime() || 0;
}

function formatTime(ts: string): string {
  if (!ts) return "";
  try {
    const d = new Date(ts.endsWith("Z") ? ts : ts + "Z");
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

function buildTimeline(detail: SessionDetail): TimelineEntry[] {
  const items: (TimelineEntry & { time: number })[] = [
    ...detail.calls.map((c) => ({ kind: "call" as const, time: toMs(c.created_at), data: c })),
    ...detail.messages.map((m) => ({ kind: "message" as const, time: toMs(m.created_at), data: m })),
    ...detail.summaries.map((s) => ({ kind: "summary" as const, time: toMs(s.created_at), data: s })),
  ];
  return items.sort((a, b) => {
    const timeDiff = b.time - a.time;
    if (timeDiff !== 0) return timeDiff;
    // Same timestamp: summaries after other items
    const kindOrder = { message: 0, call: 0, summary: 1 };
    return (kindOrder[a.kind] ?? 0) - (kindOrder[b.kind] ?? 0);
  });
}

function ChatBubble({ msg, isGroup }: { msg: MessageItem; isGroup: boolean }) {
  const isIncoming = msg.role === "user";
  const isSystem = msg.role === "system";

  if (isSystem) {
    return <div className="text-center text-[10px] text-muted/50 py-1">{msg.content}</div>;
  }

  return (
    <div className={`flex ${isIncoming ? "justify-start" : "justify-end"} mb-1`}>
      <div
        className={`max-w-[85%] px-2.5 py-1.5 text-xs whitespace-pre-wrap break-words ${
          isIncoming
            ? "bg-[#2a2a2e] text-text rounded-l-sm rounded-tr-sm"
            : "bg-accent/20 text-text rounded-r-sm rounded-tl-sm"
        }`}
      >
        {isGroup && isIncoming && msg.sender_name && (
          <div className="text-[9px] text-accent/70 mb-0.5">{msg.sender_name}</div>
        )}
        <div className="line-clamp-12">{msg.content}</div>
        <div className="text-[9px] text-muted/40 mt-0.5 text-right">{formatTime(msg.created_at)}</div>
      </div>
    </div>
  );
}

function CollapsedCallCard({ call, sessionKey }: { call: CallItem; sessionKey: string }) {
  return (
    <Link
      to="/sessions/$sessionKey/calls/$callId"
      params={{ sessionKey, callId: call.id }}
      className="block mx-4 my-1 bg-surface/50 border border-border/50 px-2.5 py-1.5 hover:bg-surface transition-colors"
    >
      <div className="flex items-center gap-2 text-[10px] text-muted">
        <span
          className={`inline-block w-1.5 h-1.5 rounded-full ${
            call.status === "completed"
              ? "bg-success/60"
              : call.status === "running"
                ? "bg-accent animate-pulse"
                : "bg-error/60"
          }`}
        />
        <span className="uppercase">{call.call_category}</span>
        <span className={call.status === "completed" ? "text-success" : "text-error"}>
          {call.status}
        </span>
        {call.latency_seconds != null && <span>{call.latency_seconds.toFixed(1)}s</span>}
        {(call.prompt_tokens != null || call.completion_tokens != null) && (
          <span>
            {call.prompt_tokens ?? 0}in/{call.completion_tokens ?? 0}out
          </span>
        )}
        {call.tool_count > 0 && <span>{call.tool_count} tools</span>}
        <span className="ml-auto text-muted">&rsaquo;</span>
      </div>
      {call.error_message && <div className="text-[10px] text-error mt-0.5">{call.error_message}</div>}
      <div className="text-[9px] text-muted/40 mt-0.5">{formatTime(call.created_at)}</div>
    </Link>
  );
}

function InlineSummaryCard({ summary }: { summary: SummaryItem }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="mx-2 my-1 bg-accent/5 border-l-2 border-accent/30 px-2.5 py-1.5">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1 w-full text-left"
      >
        <span className="text-[10px] text-accent/70">summary</span>
        <span className="text-[10px] text-muted">{summary.message_count} msgs</span>
        <span className="text-[10px] text-muted ml-auto">{expanded ? "collapse" : "expand"}</span>
      </button>
      {expanded && (
        <div className="text-xs text-text mt-1">
          <RichText text={summary.summary_text} />
        </div>
      )}
    </div>
  );
}

function ReflectionCard({ call, sessionKey }: { call: CallItem; sessionKey: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="mx-4 my-1 bg-accent/5 border-l-2 border-accent/30 px-2.5 py-1.5">
      <div className="flex items-center gap-2 text-[10px] text-accent/70 mb-0.5">
        <span>reflection</span>
        <span className={call.status === "completed" ? "text-success" : "text-error"}>
          {call.status}
        </span>
        {call.latency_seconds != null && <span>{call.latency_seconds.toFixed(1)}s</span>}
        <Link
          to="/sessions/$sessionKey/calls/$callId"
          params={{ sessionKey, callId: call.id }}
          className="text-muted hover:text-accent ml-auto"
          onClick={(e) => e.stopPropagation()}
        >
          &rsaquo;
        </Link>
      </div>
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
      {call.error_message && <div className="text-xs text-error mt-0.5">{call.error_message}</div>}
      <button onClick={() => setExpanded(!expanded)} className="text-[10px] text-accent/70 hover:text-accent mt-1">
        {expanded ? "collapse" : "expand"}
      </button>
    </div>
  );
}

function SessionDetailPage() {
  const { sessionKey } = Route.useParams();
  const queryClient = useQueryClient();
  const [viewMode, setViewMode] = useState<"messages" | "calls">("messages");
  const [reflectOpen, setReflectOpen] = useState(false);
  const [reflectQuery, setReflectQuery] = useState("");
  const [editingAgenda, setEditingAgenda] = useState(false);
  const [editAgenda, setEditAgenda] = useState("");

  const { data: detail } = useQuery<SessionDetail>({
    queryKey: ["session-detail", sessionKey],
    queryFn: () => fetchAPI<SessionDetail>(`/sessions/${encodeURIComponent(sessionKey)}`),
  });

  const liveRunning = useRef<Map<string, CallItem>>(new Map());
  const liveTools = useRef<Map<string, string[]>>(new Map());
  const wsEvents = useWSEvents();
  const _lastEvent = wsEvents[0];

  if (_lastEvent && _lastEvent.payload?.session_key === sessionKey) {
    const evt = _lastEvent;
    if (evt.type === "llm.call.running") {
      const key = `${evt.payload.session_key}-${evt.timestamp}`;
      if (!liveRunning.current.has(key)) {
        liveRunning.current.set(key, {
          id: `live-${Date.now()}`,
          created_at: new Date().toISOString(),
          call_category: (evt.payload.call_category as string) || "",
          status: "running",
          latency_seconds: null,
          ttft_seconds: null,
          total_tokens: null,
          prompt_tokens: null,
          completion_tokens: null,
          tool_count: 0,
          model: (evt.payload.model as string) || "",
          user_message: "",
          response_preview: "",
          error_message: null,
          contact_id: null,
          contact_name: null,
          tools: [],
        });
        liveTools.current.set(key, []);
      }
    } else if (evt.type === "llm.call.tool_completed") {
      const runningKey = [...liveRunning.current.keys()].pop();
      if (runningKey) {
        const tools = liveTools.current.get(runningKey) || [];
        tools.push((evt.payload.tool_name as string) || "unknown");
        liveTools.current.set(runningKey, tools);
        const call = liveRunning.current.get(runningKey);
        if (call) call.tools = [...tools];
      }
    } else if (evt.type === "llm.call.completed" || evt.type === "llm.call.failed") {
      if (liveRunning.current.size > 0) {
        liveRunning.current.clear();
        liveTools.current.clear();
        queryClient.invalidateQueries({ queryKey: ["session-detail", sessionKey] });
      }
    }
  }

  const mergedDetail = useMemo(() => {
    if (!detail) return detail;
    const liveCalls = [...liveRunning.current.values()];
    if (liveCalls.length === 0) return detail;
    return { ...detail, calls: [...liveCalls, ...detail.calls] };
  }, [detail, liveRunning.current.size, liveTools.current.size]);

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

  if (!mergedDetail) {
    return <div className="p-4 text-muted text-center text-xs">loading...</div>;
  }

  const ctx = mergedDetail.session_context;
  const isGroup = ctx?.kind === "group";
  const displayName = ctx?.display_name || mergedDetail.session_key;
  const timeline = buildTimeline(mergedDetail);

  return (
    <div className="flex flex-col gap-3 p-3">
      {/* Header */}
      <div>
        <Link to="/sessions" className="text-xs text-accent hover:underline">&larr; sessions</Link>
        <h1 className="text-sm font-medium mt-1 break-all">{displayName}</h1>
        {ctx?.display_name && ctx.display_name !== mergedDetail.session_key && (
          <div className="text-[10px] text-muted/50 break-all">{mergedDetail.session_key}</div>
        )}
        <div className="flex items-center gap-2 mt-1 text-[10px] text-muted">
          <span className="uppercase">{mergedDetail.channel}</span>
          {ctx?.kind && <span className="bg-surface border border-border px-1">{ctx.kind}</span>}
          {ctx?.member_count != null && <span>{ctx.member_count} members</span>}
          <span>{mergedDetail.stats.total_calls} calls</span>
          <span className="text-success">{mergedDetail.stats.completed} ok</span>
          {mergedDetail.stats.failed > 0 && <span className="text-error">{mergedDetail.stats.failed} failed</span>}
        </div>
      </div>

      {mergedDetail.participants.length > 0 && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">participants</h2>
          {mergedDetail.participants.map((p) => (
            <div key={p.identifier} className="flex items-center gap-1 text-xs py-0.5">
              <span className={`w-1.5 h-1.5 rounded-full ${p.is_trusted ? "bg-success" : "bg-muted"}`} />
              {p.contact_id ? (
                <Link
                  to="/contacts/$contactId"
                  params={{ contactId: p.contact_id }}
                  className="text-text hover:underline"
                >
                  {p.display_name}
                </Link>
              ) : (
                <span className="text-text">{p.display_name}</span>
              )}
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
              onClick={() => { setEditAgenda(mergedDetail.current_agenda ?? ""); setEditingAgenda(true); }}
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
              {agendaMutation.isError && <span className="text-[10px] text-error">save failed</span>}
            </div>
          </div>
        ) : mergedDetail.current_agenda ? (
          <div className="text-xs text-text bg-surface border border-border p-2 whitespace-pre-wrap">
            {mergedDetail.current_agenda}
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
              {reflectMutation.isError && <span className="text-[10px] text-error">reflection failed</span>}
            </div>
          </div>
        )}
      </section>

      {/* View toggle */}
      <div className="flex border border-border">
        <button
          onClick={() => setViewMode("messages")}
          className={`flex-1 text-[10px] py-1 ${
            viewMode === "messages" ? "bg-accent text-bg" : "text-muted hover:text-text"
          }`}
        >
          messages
        </button>
        <button
          onClick={() => setViewMode("calls")}
          className={`flex-1 text-[10px] py-1 ${
            viewMode === "calls" ? "bg-accent text-bg" : "text-muted hover:text-text"
          }`}
        >
          calls
        </button>
      </div>

      {/* Timeline */}
      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">
          {viewMode === "messages"
            ? `conversation (${mergedDetail.messages.length} messages)`
            : `calls (${mergedDetail.calls.length})`}
        </h2>
        {timeline
          .filter((entry) => viewMode === "messages" || entry.kind === "call")
          .map((entry) =>
            entry.kind === "message" ? (
              <ChatBubble key={`m-${entry.data.id}`} msg={entry.data} isGroup={isGroup} />
            ) : entry.kind === "summary" ? (
              viewMode === "messages" ? (
                <InlineSummaryCard key={`s-${entry.data.id}`} summary={entry.data} />
              ) : null
            ) : entry.data.call_category === "reflection" ? (
              <ReflectionCard key={`c-${entry.data.id}`} call={entry.data} sessionKey={sessionKey} />
            ) : entry.data.status === "running" ? (
              <CollapsedCallCard key={`c-${entry.data.id}`} call={entry.data} sessionKey={sessionKey} />
            ) : (
              <CollapsedCallCard key={`c-${entry.data.id}`} call={entry.data} sessionKey={sessionKey} />
            ),
          )}
      </section>
    </div>
  );
}

export const Route = createFileRoute("/sessions/$sessionKey/")({ component: SessionDetailPage });
