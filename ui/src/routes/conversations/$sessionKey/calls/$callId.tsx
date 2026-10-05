import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { fetchAPI } from "@/lib/api";
import { useWSEvents } from "@/hooks/use-live-data";

interface ChatMessage {
  role: string;
  content: string;
}

interface ToolCallItem {
  type: "function_call";
  call_id: string;
  name: string;
  arguments: string;
}

interface ToolOutputItem {
  type: "function_call_output";
  call_id: string;
  output: string;
}

interface WebSearchItem {
  type: "web_search_call";
  id: string;
  status: string;
}

type MessageItem = ChatMessage | ToolCallItem | ToolOutputItem | WebSearchItem;

interface CallDetail {
  id: string;
  created_at: string;
  provider: string;
  model: string;
  call_category: string;
  session_key: string;
  status: string;
  latency_seconds: number | null;
  ttft_seconds: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  cached_tokens: number | null;
  messages: MessageItem[] | null;
  tool_calls: MessageItem[] | null;
  tools: { type: string; name: string; description: string; parameters?: Record<string, unknown> }[] | null;
  response_text: string;
  user_message: string;
  system_prompt: string;
  error_message: string | null;
  generation_id: string | null;
  served_by: string | null;
  served_quant: string | null;
  reasoning_effort: string | null;
}

// Durable trace timeline (GET /calls/{id}/trace) + live stream state.
// Trace kinds: reasoning_part | tool_call | tool_result | round_completed | turn_note
interface TraceEvent {
  id: string;
  iteration: number;
  seq: number;
  kind: string;
  content: string;
  meta: Record<string, unknown> | null;
  created_at: string;
}

interface TraceResponse {
  call_id: string;
  status: string;
  model: string;
  session_key: string;
  events: TraceEvent[];
}

interface LiveToolCardState {
  itemKey: string;
  name: string | null;
  args: string;
  output?: string;
}

interface LiveRoundState {
  iteration: number;
  reasoning: string;
  text: string;
  tools: LiveToolCardState[];
  roundDone: { latency: number | null; toolCalls: number | null } | null;
}

function isChat(m: MessageItem): m is ChatMessage {
  return "role" in m;
}
function isToolCall(m: MessageItem): m is ToolCallItem {
  return "type" in m && m.type === "function_call";
}
function isToolOutput(m: MessageItem): m is ToolOutputItem {
  return "type" in m && m.type === "function_call_output";
}
function isWebSearch(m: MessageItem): m is WebSearchItem {
  return "type" in m && m.type === "web_search_call";
}

function stripMetadataEnvelope(text: string): string {
  return text.replace(/^#{2,} .*\n(?:(?!#{2,} ).*\S.*\n)*\n/, "").trimStart();
}

function Collapsible({ title, defaultOpen = false, children }: { title: string; defaultOpen?: boolean; children: React.ReactNode }) {
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

function MessageBubble({ role, content }: { role: string; content: string }) {
  const colors: Record<string, string> = {
    system: "bg-accent/10 border-accent/30 text-text",
    user: "bg-surface border-border text-text",
    assistant: "bg-surface border-border text-text",
    tool: "bg-muted/10 border-border text-muted",
  };
  const displayContent = role === "user" ? stripMetadataEnvelope(content) : content;
  return (
    <div className={`border p-2 text-xs whitespace-pre-wrap break-words ${colors[role] ?? "bg-surface border-border text-text"}`}>
      <div className="text-[9px] text-muted uppercase mb-0.5">{role}</div>
      <div className="line-clamp-20">{displayContent || "[empty]"}</div>
    </div>
  );
}

function ThinkingBlock({ text, streaming, title }: { text: string; streaming: boolean; title?: string }) {
  if (!text.trim()) return null;
  return (
    <div className="bg-muted/5 border border-border/60 p-2">
      <div className="text-[9px] text-muted uppercase mb-0.5 flex items-center gap-1">
        {streaming && <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />}
        {title ?? "thinking"}
      </div>
      <div className="text-[11px] text-muted whitespace-pre-wrap break-words max-h-64 overflow-y-auto">{text}</div>
    </div>
  );
}

function ToolCard({ name, args, output, streaming }: { name: string | null; args?: string; output?: string; streaming?: boolean }) {
  let displayArgs = args;
  try {
    if (args) displayArgs = JSON.stringify(JSON.parse(args), null, 2);
  } catch { /* keep raw */ }
  return (
    <div className="bg-surface border border-accent/30">
      <div className="p-2 border-b border-border">
        <div className="text-xs text-accent font-medium">{name ?? "(tool)"}</div>
        {displayArgs && (
          <div className="text-xs text-text mt-1 whitespace-pre-wrap break-words">{displayArgs}</div>
        )}
      </div>
      {output ? (
        <div className="p-2">
          <div className="text-[9px] text-muted uppercase mb-0.5">output</div>
          <div className="text-xs text-muted whitespace-pre-wrap break-words">{output}</div>
        </div>
      ) : (
        <div className="p-2">
          <div className="text-[9px] text-muted flex items-center gap-1">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
            {streaming ? "running..." : "waiting..."}
          </div>
        </div>
      )}
    </div>
  );
}

/** One round from the durable trace: reasoning parts, tool call/result
 * pairs, round meta. Collapsed by default unless it's the newest round. */
function RoundSection({ events, iteration, newest }: { events: TraceEvent[]; iteration: number; newest: boolean }) {
  const reasoning = events
    .filter((e) => e.kind === "reasoning_part")
    .map((e) => e.content)
    .join("\n\n");
  const toolCalls = events.filter((e) => e.kind === "tool_call");
  const results = events.filter((e) => e.kind === "tool_result");
  const done = events.find((e) => e.kind === "round_completed");
  const note = events.find((e) => e.kind === "turn_note");
  const usedResults = new Set<number>();
  const resultFor = (tc: TraceEvent): TraceEvent | undefined => {
    const name = (tc.meta?.name as string) ?? null;
    const idx = results.findIndex(
      (r, i) => !usedResults.has(i) && (name === null || ((r.meta?.name as string) ?? null) === name));
    if (idx === -1) return undefined;
    usedResults.add(idx);
    return results[idx];
  };
  const meta = (done?.meta ?? {}) as Record<string, unknown>;
  const latency = typeof meta.latency_seconds === "number" ? meta.latency_seconds : null;
  const tokens = typeof meta.completion_tokens === "number" ? meta.completion_tokens : null;
  return (
    <div className="border border-border/60">
      <div className="flex items-center gap-2 px-2 py-1 bg-muted/5 text-[10px] text-muted">
        <span className="uppercase tracking-wider">round {iteration}</span>
        {latency !== null && <span>{latency.toFixed(2)}s</span>}
        {tokens !== null && <span>{tokens} tok out</span>}
        {toolCalls.length > 0 && <span>{toolCalls.length} tool call{toolCalls.length > 1 ? "s" : ""}</span>}
        {!done && <span className="text-accent animate-pulse">in flight…</span>}
      </div>
      <div className="p-2 flex flex-col gap-1">
        {reasoning.length > 0 && (
          <Collapsible title={`reasoning (${reasoning.length} chars)`} defaultOpen={newest}>
            <ThinkingBlock text={reasoning} streaming={false} />
          </Collapsible>
        )}
        {toolCalls.map((tc, i) => {
          let name = (tc.meta?.name as string) ?? "(tool)";
          let args: string | undefined;
          try {
            const parsed = JSON.parse(tc.content);
            name = parsed.name ?? name;
            args = typeof parsed.arguments === "string" ? parsed.arguments : JSON.stringify(parsed.arguments);
          } catch { args = tc.content; }
          const res = resultFor(tc);
          return <ToolCard key={i} name={name} args={args} output={res?.content} />;
        })}
        {note && <div className="text-[10px] text-muted italic">{note.content}</div>}
      </div>
    </div>
  );
}

function CallDetailPage() {
  const { sessionKey, callId } = Route.useParams();
  const queryClient = useQueryClient();

  const { data: call } = useQuery<CallDetail>({
    queryKey: ["call-detail", callId],
    queryFn: () => fetchAPI<CallDetail>(`/calls/${callId}`),
    refetchInterval: (query) =>
      query.state.data?.status === "running" ? 5000 : false,
  });

  // Durable trace: backfill on load, replay source, and the fallback view
  // when the WS wasn't connected for the whole turn.
  const { data: trace } = useQuery<TraceResponse>({
    queryKey: ["call-trace", callId],
    queryFn: () => fetchAPI<TraceResponse>(`/calls/${callId}/trace`),
    refetchInterval: (query) =>
      call?.status === "running" || query.state.data?.status === "running" ? 5000 : false,
  });

  const liveRef = useRef<LiveRoundState | null>(null);
  const processedEvents = useRef<Set<unknown>>(new Set());
  const [, setTick] = useState(0);
  const wsEvents = useWSEvents();
  useEffect(() => {
    setTick((t) => t + 1);
  }, [wsEvents]);

  const isRunning = call?.status === "running";

  // Live tail: completed rounds come from the trace poll; only the CURRENT
  // round renders from stream events — no dedup needed between the two.
  if (isRunning) {
    for (const evt of [...wsEvents].reverse()) {
      if (processedEvents.current.has(evt)) continue;
      processedEvents.current.add(evt);
      const p = (evt.payload ?? {}) as Record<string, unknown>;
      if (p.log_id !== callId) continue;
      if (evt.type === "llm.stream.round") {
        const phase = p.phase as string;
        if (phase === "started") {
          liveRef.current = {
            iteration: (p.iteration as number) ?? 0,
            reasoning: "", text: "", tools: [], roundDone: null,
          };
        } else if (phase === "completed" && liveRef.current) {
          liveRef.current.roundDone = {
            latency: (p.latency_seconds as number) ?? null,
            toolCalls: (p.tool_calls as number) ?? null,
          };
        }
      } else if (evt.type === "llm.stream.reasoning") {
        const live = liveRef.current ?? (liveRef.current = {
          iteration: (p.iteration as number) ?? 0,
          reasoning: "", text: "", tools: [], roundDone: null,
        });
        live.reasoning += (p.text as string) ?? "";
        if (p.done) live.reasoning += "\n\n";
      } else if (evt.type === "llm.stream.text") {
        const live = liveRef.current ?? (liveRef.current = {
          iteration: (p.iteration as number) ?? 0,
          reasoning: "", text: "", tools: [], roundDone: null,
        });
        live.text += (p.text as string) ?? "";
      } else if (evt.type === "llm.stream.tool") {
        const live = liveRef.current ?? (liveRef.current = {
          iteration: (p.iteration as number) ?? 0,
          reasoning: "", text: "", tools: [], roundDone: null,
        });
        const phase = p.phase as string;
        if (phase === "started") {
          live.tools.push({
            itemKey: (p.item_id as string) ?? String(live.tools.length),
            name: (p.name as string) ?? null, args: "",
          });
        } else if (phase === "args") {
          const card = live.tools.find((t) => t.itemKey === p.item_id)
            ?? live.tools[live.tools.length - 1];
          if (card) card.args += (p.args as string) ?? "";
        }
      } else if (evt.type === "llm.call.tool_completed") {
        const live = liveRef.current;
        const name = p.tool_name as string | undefined;
        if (live && name) {
          const card = live.tools.find((t) => t.name === name && t.output === undefined)
            ?? live.tools.find((t) => t.output === undefined);
          if (card) card.output = (p.tool_output as string) ?? "";
        }
      } else if (evt.type === "llm.call.completed" || evt.type === "llm.call.failed") {
        queryClient.invalidateQueries({ queryKey: ["call-detail", callId] });
        queryClient.invalidateQueries({ queryKey: ["call-trace", callId] });
        queryClient.invalidateQueries({ queryKey: ["session-detail", sessionKey] });
      }
    }
  }

  if (!call) {
    return <div className="p-4 text-muted text-center text-xs">loading...</div>;
  }

  if ("error" in call) {
    return (
      <div className="flex flex-col gap-3 p-3">
        <Link to="/conversations/$sessionKey" params={{ sessionKey }} className="text-xs text-accent hover:underline">
          &larr; session
        </Link>
        <div className="text-xs text-error text-center">call not found</div>
      </div>
    );
  }

  const allMsgs = call.messages ?? [];
  const priorMessages = allMsgs.filter((m) => isChat(m) && m.role !== "system").slice(0, -1) as ChatMessage[];
  // Tool calls come ONLY from this turn's own tool_blocks_json — messages
  // is the wire context and carries REPLAYED history tool blocks from prior
  // turns (2026-10-04: a zero-tool turn showed a week-old call here). Live
  // tools while running render in the turn timeline above.
  const toolItems = call.tool_calls ?? [];
  const toolCalls = toolItems.filter(isToolCall);
  const toolOutputs = toolItems.filter(isToolOutput);
  const webSearches = allMsgs.filter(isWebSearch);

  // Group durable trace rows into rounds (by iteration, seq order).
  const traceEvents = trace?.events ?? [];
  const rounds: { iteration: number; events: TraceEvent[] }[] = [];
  const loose: TraceEvent[] = []; // turn_note etc. with no round
  for (const e of traceEvents) {
    if (e.kind === "turn_note") { loose.push(e); continue; }
    let round = rounds.find((r) => r.iteration === e.iteration);
    if (!round) {
      round = { iteration: e.iteration, events: [] };
      rounds.push(round);
    }
    round.events.push(e);
  }
  rounds.sort((a, b) => a.iteration - b.iteration);

  // The live round is hidden once the trace poll has persisted it.
  const live = liveRef.current;
  const liveVisible = isRunning && live !== null
    && !rounds.some((r) => r.iteration >= live.iteration && r.events.some((e) => e.kind === "round_completed"));

  return (
    <div className="flex flex-col gap-3 p-3">
      <div>
        <Link to="/conversations/$sessionKey" params={{ sessionKey }} className="text-xs text-accent hover:underline">
          &larr; session
        </Link>
        <div className="flex items-center gap-2 mt-1 text-[10px] text-muted flex-wrap">
          <span className="uppercase">{call.call_category}</span>
          <span>{call.model}</span>
          {call.served_by && <span>via {call.served_by}{call.served_quant ? ` (${call.served_quant})` : ""}</span>}
          {call.reasoning_effort && <span>effort {call.reasoning_effort}</span>}
          <span className={call.status === "completed" ? "text-success" : isRunning ? "text-accent animate-pulse" : "text-error"}>
            {call.status}
          </span>
          {call.latency_seconds != null && <span>{call.latency_seconds.toFixed(2)}s</span>}
          {call.ttft_seconds != null && <span>ttft {call.ttft_seconds.toFixed(2)}s</span>}
          {call.total_tokens != null && <span>{call.total_tokens} tok</span>}
          {call.cached_tokens != null && call.cached_tokens > 0 && <span>({call.cached_tokens} cached)</span>}
        </div>
      </div>

      {call.error_message && (
        <div className="text-xs text-error bg-error/10 border border-error/30 p-2 whitespace-pre-wrap">
          {call.error_message}
        </div>
      )}

      {(rounds.length > 0 || liveVisible) && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">
            turn timeline ({rounds.length} round{rounds.length === 1 ? "" : "s"}
            {liveVisible ? ", 1 live" : ""})
          </h2>
          <div className="flex flex-col gap-1">
            {rounds.map((r, i) => (
              <RoundSection key={r.iteration} events={r.events} iteration={r.iteration} newest={i === rounds.length - 1} />
            ))}
            {liveVisible && live && (
              <div className="border border-accent/40">
                <div className="flex items-center gap-2 px-2 py-1 bg-accent/5 text-[10px] text-accent">
                  <span className="uppercase tracking-wider">round {live.iteration}</span>
                  <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
                  {live.roundDone
                    ? `done${live.roundDone.latency != null ? ` in ${live.roundDone.latency.toFixed(2)}s` : ""}`
                    : "streaming…"}
                </div>
                <div className="p-2 flex flex-col gap-1">
                  <ThinkingBlock text={live.reasoning} streaming={!live.roundDone} />
                  {live.tools.map((t, i) => (
                    <ToolCard key={t.itemKey + i} name={t.name} args={t.args || undefined} output={t.output} streaming={!live.roundDone} />
                  ))}
                  {live.text && (
                    <div className="bg-surface border border-border p-2 text-xs whitespace-pre-wrap break-words">
                      <div className="text-[9px] text-muted uppercase mb-0.5">final text</div>
                      {live.text}
                    </div>
                  )}
                </div>
              </div>
            )}
            {loose.map((n) => (
              <div key={n.id} className="text-[10px] text-muted italic">{n.content}</div>
            ))}
          </div>
        </section>
      )}

      {priorMessages.length > 0 && (
        <Collapsible title={`message history (${priorMessages.length})`}>
          <div className="flex flex-col gap-px max-h-64 overflow-y-auto">
            {priorMessages.map((m, i) => (
              <MessageBubble key={i} role={m.role} content={m.content ?? ""} />
            ))}
          </div>
        </Collapsible>
      )}

      {call.system_prompt && (
        <Collapsible title="system prompt">
          <div className="bg-accent/5 border border-accent/20 p-2 text-xs whitespace-pre-wrap break-words max-h-64 overflow-y-auto">
            {call.system_prompt}
          </div>
        </Collapsible>
      )}

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">user message</h2>
        <div className="bg-surface border border-border p-2 text-xs whitespace-pre-wrap break-words">
          {call.user_message ? stripMetadataEnvelope(call.user_message) : "[empty]"}
        </div>
      </section>

      {call.response_text && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">response</h2>
          <div className="bg-surface border border-border p-2 text-xs whitespace-pre-wrap break-words">
            {call.response_text}
          </div>
        </section>
      )}

      {(toolCalls.length > 0 || webSearches.length > 0) && (
        <Collapsible title={`tool calls (${webSearches.length + toolCalls.length})`}>
          <div className="flex flex-col gap-1">
            {webSearches.map((ws) => (
              <div key={ws.id} className="bg-surface border border-border p-2">
                <div className="text-[9px] text-muted uppercase">web search</div>
                <div className="text-xs text-text">{ws.status}</div>
              </div>
            ))}
            {toolCalls.map((tc) => {
              const output = toolOutputs.find((o) => o.call_id === tc.call_id);
              let args = tc.arguments;
              try { args = JSON.stringify(JSON.parse(args), null, 2); } catch { /* keep raw */ }
              return (
                <div key={tc.call_id} className="bg-surface border border-border">
                  <div className="p-2 border-b border-border">
                    <div className="text-xs text-accent font-medium">{tc.name}</div>
                    <div className="text-xs text-text mt-1 whitespace-pre-wrap break-words">{args}</div>
                  </div>
                  {output && (
                    <div className="p-2">
                      <div className="text-[9px] text-muted uppercase mb-0.5">output</div>
                      <div className="text-xs text-muted whitespace-pre-wrap break-words">{output.output}</div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </Collapsible>
      )}

      {call.tools && call.tools.length > 0 && (
        <Collapsible title={`tools offered (${call.tools.length})`}>
          <div className="flex flex-col gap-px">
            {call.tools.map((t) => (
              <div key={t.name} className="bg-surface border border-border p-2">
                <div className="text-xs text-text font-medium">{t.name}</div>
                <div className="text-[10px] text-muted">{t.description}</div>
              </div>
            ))}
          </div>
        </Collapsible>
      )}
    </div>
  );
}

export const Route = createFileRoute("/conversations/$sessionKey/calls/$callId")({ component: CallDetailPage });
