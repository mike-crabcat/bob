import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchAPI, postAPI } from "@/lib/api";
import { parseTs } from "@/lib/time";

interface CallDetail {
  id: string;
  call_sid: string;
  phone_number: string;
  direction: string;
  status: string;
  agenda: string;
  exchange_count: number;
  duration_seconds: number | null;
  recording_path: string | null;
  started_at: string;
  completed_at: string | null;
  contact_id: string | null;
  contact_name: string | null;
  transcript: string | null;
  outcome: CallOutcome | null;
}

interface CallOutcome {
  tool: "report_success" | "report_failure" | string;
  summary?: string;
  reason?: string;
  details?: string;
}

interface Exchange {
  exchange_index: number;
  user_transcript: string;
  assistant_transcript: string;
  stt_ms: number | null;
  llm_total_ms: number | null;
  tts_first_chunk_ms: number | null;
  e2e_ms: number | null;
  started_at: string | null;
  created_at: string | null;
}

function formatTime(ts: string | null): string {
  if (!ts) return "";
  const d = parseTs(ts);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleString();
}

function formatDuration(seconds: number | null): string {
  if (seconds == null) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    ringing: "bg-yellow-500/20 text-yellow-600",
    active: "bg-green-500/20 text-green-600",
    completed: "bg-surface text-muted",
    failed: "bg-red-500/20 text-red-600",
    busy: "bg-orange-500/20 text-orange-600",
    "no-answer": "bg-orange-500/20 text-orange-600",
    canceled: "bg-surface text-muted",
  };
  return (
    <span className={`text-[9px] px-1.5 py-0.5 ${colors[status] || "bg-surface text-muted"}`}>
      {status}
    </span>
  );
}

interface TranscriptTurn {
  speaker: "agent" | "user";
  text: string;
}

function parseTranscript(transcript: string): TranscriptTurn[] {
  const turns: TranscriptTurn[] = [];
  for (const line of transcript.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed.startsWith("Agent:")) {
      turns.push({ speaker: "agent", text: trimmed.slice(6).trim() });
    } else if (trimmed.startsWith("User:")) {
      turns.push({ speaker: "user", text: trimmed.slice(5).trim() });
    } else if (turns.length > 0) {
      // continuation of the previous turn
      turns[turns.length - 1].text += `\n${trimmed}`;
    } else {
      turns.push({ speaker: "agent", text: trimmed });
    }
  }
  return turns;
}

function LiveTranscript({ transcript, active }: { transcript: string; active: boolean }) {
  const turns = parseTranscript(transcript);
  return (
    <section>
      <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1 flex items-center gap-2">
        transcript
        {active && (
          <span className="flex items-center gap-1 text-[9px] text-green-600 normal-case">
            <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
            live
          </span>
        )}
      </h2>
      <div className="flex flex-col gap-1">
        {turns.map((turn, i) => (
          <div key={i} className="border-l-2 border-border pl-2 py-1 whitespace-pre-wrap">
            <span
              className={`text-xs font-medium ${turn.speaker === "agent" ? "text-accent" : "text-muted"}`}
            >
              {turn.speaker === "agent" ? "bob" : "them"}:{" "}
            </span>
            <span className="text-xs text-text">{turn.text}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function OutcomeBlock({ outcome }: { outcome: CallOutcome }) {
  const success = outcome.tool === "report_success";
  return (
    <section>
      <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">outcome</h2>
      <div className="text-xs bg-surface border border-border p-2">
        <span className={`font-medium ${success ? "text-green-600" : "text-red-600"}`}>
          {success ? "✓ success" : "✗ failure"}
        </span>
        {outcome.summary && <div className="mt-1 text-text">{outcome.summary}</div>}
        {outcome.reason && <div className="mt-1 text-text">{outcome.reason}</div>}
        {outcome.details && (
          <div className="mt-1 text-muted whitespace-pre-wrap">{outcome.details}</div>
        )}
      </div>
    </section>
  );
}

function CallDetailPage() {
  const { callId } = Route.useParams();
  const queryClient = useQueryClient();
  const base = import.meta.env.BASE_URL.replace(/\/$/, "");

  const { data } = useQuery<{
    call: CallDetail;
    exchanges: Exchange[];
  }>({
    queryKey: ["phone-call", callId],
    queryFn: () =>
      fetchAPI<{ call: CallDetail; exchanges: Exchange[] }>(
        `/phone/calls/${encodeURIComponent(callId)}`,
      ),
    refetchInterval: (query) => {
      const status = query.state.data?.call?.status;
      return status === "active" || status === "ringing" ? 3000 : false;
    },
  });

  const hangupMutation = useMutation({
    mutationFn: () => postAPI(`/phone/calls/${encodeURIComponent(callId)}/hangup`, {}),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["phone-call", callId] }),
  });

  if (!data) {
    return <div className="p-4 text-muted text-center text-xs">loading...</div>;
  }

  const { call, exchanges } = data;

  const sessionKey = `agent:main:phone:call:${call.id}`;

  const secret = document.cookie.match(/bob_dashboard_secret=([^;]+)/)?.[1] || "";
  const recordingUrl = `${base}/api/phone/recording/${callId}?secret=${encodeURIComponent(secret)}`;

  return (
    <div className="flex flex-col gap-3 p-3">
      <div>
        <Link to="/phone" className="text-xs text-accent hover:underline">
          &larr; phone
        </Link>
        <div className="flex items-center gap-2 mt-1">
          <h1 className="text-sm font-medium">
            {call.contact_name || call.phone_number}
          </h1>
          <span className="text-xs text-muted">
            {call.direction === "outbound"
              ? "↗ outgoing"
              : call.direction === "voice_link"
                ? "◆ voice link"
                : "↙ incoming"}
          </span>
          <StatusBadge status={call.status} />
        </div>
        <div className="flex items-center gap-3 mt-1 text-[10px] text-muted">
          <span>{formatTime(call.started_at)}</span>
          {call.duration_seconds != null && (
            <span>{formatDuration(call.duration_seconds)}</span>
          )}
          <span>{call.exchange_count} exchanges</span>
        </div>
      </div>

      {(call.status === "active" || call.status === "ringing") && call.direction !== "voice_link" && (
        <button
          onClick={() => hangupMutation.mutate()}
          disabled={hangupMutation.isPending}
          className="w-full py-2 text-xs font-medium text-white bg-red-600 hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed rounded"
        >
          {hangupMutation.isPending ? "hanging up..." : "hang up"}
        </button>
      )}

      {call.agenda && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">agenda</h2>
          <div className="text-xs text-text bg-surface border border-border p-2 whitespace-pre-wrap">
            {call.agenda}
          </div>
        </section>
      )}

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">session</h2>
        <Link
          to="/sessions/$sessionKey"
          params={{ sessionKey }}
          className="text-xs text-accent hover:underline break-all"
        >
          {sessionKey}
        </Link>
      </section>

      {(call.transcript || call.status === "active") && (
        call.transcript ? (
          <LiveTranscript transcript={call.transcript} active={call.status === "active"} />
        ) : (
          <section>
            <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1 flex items-center gap-2">
              transcript
              <span className="flex items-center gap-1 text-[9px] text-green-600 normal-case">
                <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
                listening…
              </span>
            </h2>
          </section>
        )
      )}

      {call.outcome && <OutcomeBlock outcome={call.outcome} />}

      {call.recording_path && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">recording</h2>
          <audio controls className="w-full h-8" src={recordingUrl}>
            your browser does not support audio
          </audio>
        </section>
      )}

      {exchanges.length > 0 && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">
            legacy exchanges ({exchanges.length})
          </h2>
          <div className="flex flex-col gap-1">
            {exchanges.map((ex) => (
              <div key={ex.exchange_index} className="border-l-2 border-border pl-2 py-1">
                <div className="text-[10px] text-muted mb-0.5">
                  exchange {ex.exchange_index + 1}
                  {ex.e2e_ms != null && (
                    <span className="ml-2">
                      {ex.e2e_ms}ms e2e
                    </span>
                  )}
                </div>
                {ex.user_transcript && (
                  <div className="text-xs text-text mb-0.5 whitespace-pre-wrap">
                    <span className="text-muted font-medium">them: </span>
                    {ex.user_transcript}
                  </div>
                )}
                {ex.assistant_transcript && (
                  <div className="text-xs text-text whitespace-pre-wrap">
                    <span className="text-accent font-medium">bob: </span>
                    {ex.assistant_transcript}
                  </div>
                )}
                {(ex.stt_ms != null || ex.llm_total_ms != null || ex.tts_first_chunk_ms != null) && (
                  <div className="text-[9px] text-muted mt-0.5">
                    {ex.stt_ms != null && <span>stt {ex.stt_ms}ms </span>}
                    {ex.llm_total_ms != null && <span>llm {ex.llm_total_ms}ms </span>}
                    {ex.tts_first_chunk_ms != null && <span>tts {ex.tts_first_chunk_ms}ms</span>}
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

export const Route = createFileRoute("/phone/$callId/")({ component: CallDetailPage });
