import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
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

function LiveToolCard({ name, args, output }: { name: string; args?: string; output?: string }) {
  let displayArgs = args;
  try { displayArgs = JSON.stringify(JSON.parse(args ?? ""), null, 2); } catch { /* keep raw */ }
  return (
    <div className="bg-surface border border-accent/30">
      <div className="p-2 border-b border-border">
        <div className="text-xs text-accent font-medium">{name}</div>
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
            running...
          </div>
        </div>
      )}
    </div>
  );
}

function CallDetailPage() {
  const { sessionKey, callId } = Route.useParams();
  const queryClient = useQueryClient();

  const { data: call } = useQuery<CallDetail>({
    queryKey: ["call-detail", callId],
    queryFn: () => fetchAPI<CallDetail>(`/calls/${callId}`),
  });

  const liveTools = useRef<Map<string, { name: string; args?: string; output?: string }>>(new Map());
  const wsEvents = useWSEvents();
  const _lastEvent = wsEvents[0];

  const isRunning = call?.status === "running";

  if (isRunning && _lastEvent) {
    const evt = _lastEvent;
    const evtLogId = evt.payload?.log_id as string | undefined;
    if (evtLogId === callId && evt.type === "llm.call.tool_completed") {
      const toolName = evt.payload.tool_name as string;
      const toolArgs = evt.payload.tool_args as Record<string, unknown> | undefined;
      const toolOutput = evt.payload.tool_output as string | undefined;
      liveTools.current.set(`${toolName}-${liveTools.current.size}`, {
        name: toolName,
        args: toolArgs ? JSON.stringify(toolArgs) : undefined,
        output: toolOutput,
      });
    }
    if (evt.type === "llm.call.completed" || evt.type === "llm.call.failed") {
      if (liveTools.current.size > 0) {
        liveTools.current.clear();
        queryClient.invalidateQueries({ queryKey: ["call-detail", callId] });
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
        <Link to="/sessions/$sessionKey" params={{ sessionKey }} className="text-xs text-accent hover:underline">
          &larr; session
        </Link>
        <div className="text-xs text-error text-center">call not found</div>
      </div>
    );
  }

  const allMsgs = call.messages ?? [];
  const priorMessages = allMsgs.filter((m) => isChat(m) && m.role !== "system").slice(0, -1) as ChatMessage[];
  const toolItems = call.tool_calls ?? [];
  const toolCalls = toolItems.filter(isToolCall);
  const toolOutputs = toolItems.filter(isToolOutput);
  const webSearches = allMsgs.filter(isWebSearch);
  const liveToolEntries = [...liveTools.current.values()];

  return (
    <div className="flex flex-col gap-3 p-3">
      <div>
        <Link to="/sessions/$sessionKey" params={{ sessionKey }} className="text-xs text-accent hover:underline">
          &larr; session
        </Link>
        <div className="flex items-center gap-2 mt-1 text-[10px] text-muted flex-wrap">
          <span className="uppercase">{call.call_category}</span>
          <span>{call.model}</span>
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

      {priorMessages.length > 0 && (
        <Collapsible title={`message history (${priorMessages.length})`}>
          <div className="flex flex-col gap-px max-h-64 overflow-y-auto">
            {priorMessages.map((m, i) => (
              <MessageBubble key={i} role={m.role} content={m.content ?? ""} />
            ))}
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

      {(toolCalls.length > 0 || webSearches.length > 0 || liveToolEntries.length > 0) && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">
            tool calls ({webSearches.length + toolCalls.length + liveToolEntries.length})
          </h2>
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
            {liveToolEntries.map((lt, i) => (
              <LiveToolCard key={`live-${i}`} name={lt.name} args={lt.args} output={lt.output} />
            ))}
          </div>
        </section>
      )}

      {call.system_prompt && (
        <Collapsible title="system prompt">
          <div className="bg-accent/5 border border-accent/20 p-2 text-xs whitespace-pre-wrap break-words max-h-64 overflow-y-auto">
            {call.system_prompt}
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

export const Route = createFileRoute("/sessions/$sessionKey/calls/$callId")({ component: CallDetailPage });
