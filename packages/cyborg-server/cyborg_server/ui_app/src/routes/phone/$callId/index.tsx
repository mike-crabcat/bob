import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchAPI } from "@/lib/api";

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
}

interface Exchange {
  exchange_index: number;
  user_transcript: string;
  assistant_transcript: string;
  stt_ms: number | null;
  openclaw_ms: number | null;
  tts_first_chunk_ms: number | null;
  e2e_ms: number | null;
  started_at: string | null;
  created_at: string | null;
}

function formatTime(ts: string | null): string {
  if (!ts) return "";
  const d = new Date(ts.endsWith("Z") ? ts : ts + "Z");
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

function CallDetailPage() {
  const { callId } = Route.useParams();
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

  if (!data) {
    return <div className="p-4 text-muted text-center text-xs">loading...</div>;
  }

  const { call, exchanges } = data;
  const sessionKey = `bobvoice:chat:phone:${call.id}`;

  const secret = document.cookie.match(/cyborg_dashboard_secret=([^;]+)/)?.[1] || "";
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
            {call.direction === "outbound" ? "↗ outgoing" : "↙ incoming"}
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
            transcript ({exchanges.length} exchanges)
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
                    <span className="text-accent font-medium">cyborg: </span>
                    {ex.assistant_transcript}
                  </div>
                )}
                {(ex.stt_ms != null || ex.openclaw_ms != null || ex.tts_first_chunk_ms != null) && (
                  <div className="text-[9px] text-muted mt-0.5">
                    {ex.stt_ms != null && <span>stt {ex.stt_ms}ms </span>}
                    {ex.openclaw_ms != null && <span>llm {ex.openclaw_ms}ms </span>}
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
