import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchAPI } from "@/lib/api";

interface BulletinDetail {
  id: string;
  created_at: string;
  channel_id: string;
  source_type: string;
  source_id: string;
  visibility: string;
  content: string;
  digested: boolean;
  session_range_start: string;
  session_range_end: string;
  claims: BulletinClaim[];
}

interface BulletinClaim {
  id: string;
  claim_type_key: string;
  subject_id: string;
  object_id: string | null;
  value: string | null;
  status: string;
  visibility: string;
  created_at: string;
}

const CLAIM_COLORS: Record<string, string> = {
  alias: "bg-gray-900/40 text-gray-300",
  appearance: "bg-gray-900/40 text-gray-300",
  spouse: "bg-pink-900/40 text-pink-300",
  parent: "bg-pink-900/40 text-pink-300",
  child: "bg-pink-900/40 text-pink-300",
  sibling: "bg-pink-900/40 text-pink-300",
  home_address: "bg-cyan-900/40 text-cyan-300",
  workplace: "bg-cyan-900/40 text-cyan-300",
  job: "bg-cyan-900/40 text-cyan-300",
  food_preference: "bg-orange-900/40 text-orange-300",
  drink_preference: "bg-orange-900/40 text-orange-300",
  interest: "bg-purple-900/40 text-purple-300",
  personality: "bg-purple-900/40 text-purple-300",
  language: "bg-blue-900/40 text-blue-300",
  birthday: "bg-blue-900/40 text-blue-300",
  contact_id: "bg-gray-900/40 text-gray-300",
  member: "bg-green-900/40 text-green-300",
  destination: "bg-green-900/40 text-green-300",
  start_date: "bg-blue-900/40 text-blue-300",
  end_date: "bg-blue-900/40 text-blue-300",
  task_status: "bg-yellow-900/40 text-yellow-300",
  owner: "bg-yellow-900/40 text-yellow-300",
  due_date: "bg-yellow-900/40 text-yellow-300",
  description: "bg-gray-900/40 text-gray-300",
  location: "bg-cyan-900/40 text-cyan-300",
  transport_type: "bg-cyan-900/40 text-cyan-300",
  decision: "bg-green-900/40 text-green-300",
  rationale: "bg-green-900/40 text-green-300",
  file_path: "bg-amber-900/40 text-amber-300",
  file_ref: "bg-amber-900/40 text-amber-300",
  thing_type: "bg-lime-900/40 text-lime-300",
  contact_method: "bg-blue-900/40 text-blue-300",
  dietary_restriction: "bg-orange-900/40 text-orange-300",
  hometown: "bg-cyan-900/40 text-cyan-300",
  attendee: "bg-green-900/40 text-green-300",
  organizer: "bg-green-900/40 text-green-300",
  purpose: "bg-gray-900/40 text-gray-300",
  name: "bg-gray-900/40 text-gray-300",
};

function BulletinDetailPage() {
  const { bulletinId } = Route.useParams();

  const { data: bulletin, isLoading, error } = useQuery<BulletinDetail>({
    queryKey: ["bulletin-detail", bulletinId],
    queryFn: () => fetchAPI<BulletinDetail>(`/memory/bulletins/${bulletinId}`),
    retry: false,
  });

  if (isLoading) return <div className="p-4 text-muted text-center text-xs">loading...</div>;
  if (error || !bulletin || ("error" in (bulletin as object))) {
    return (
      <div className="p-4">
        <Link to="/memory" className="text-xs text-accent hover:underline">&larr; memory</Link>
        <p className="text-xs text-red-400 mt-2">bulletin not found</p>
      </div>
    );
  }

  const createdDate = bulletin.created_at
    ? new Date(bulletin.created_at).toLocaleString()
    : "unknown";

  return (
    <div className="flex flex-col gap-3 p-3">
      <Link to="/memory" className="text-xs text-accent hover:underline">&larr; memory</Link>

      <div>
        <div className="flex items-center gap-2">
          <h1 className="text-sm font-medium font-mono">{bulletin.id}</h1>
          {bulletin.digested && (
            <span className="text-[8px] text-success/80 bg-success/10 px-1 rounded">digested</span>
          )}
        </div>
        <div className="flex items-center gap-3 mt-1 text-[10px] text-muted">
          <span>{createdDate}</span>
          <span className="uppercase">{bulletin.channel_id}</span>
          <span className="bg-accent/10 text-accent/70 px-1 rounded text-[8px]">{bulletin.source_type}</span>
          <span className="text-muted/50">vis: {bulletin.visibility}</span>
        </div>
      </div>

      {bulletin.source_id && (
        <div className="text-[10px] text-muted">
          <span className="text-muted/50">source session:</span>{" "}
          <Link
            to="/sessions/$sessionKey"
            params={{ sessionKey: bulletin.source_id }}
            className="text-accent hover:underline"
          >
            {bulletin.source_id}
          </Link>
        </div>
      )}

      {bulletin.session_range_start && (
        <div className="text-[10px] text-muted">
          <span className="text-muted/50">session range:</span>{" "}
          {bulletin.session_range_start}
          {bulletin.session_range_end && bulletin.session_range_end !== bulletin.session_range_start && (
            <> → {bulletin.session_range_end}</>
          )}
        </div>
      )}

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">content</h2>
        <pre className="text-[10px] text-text whitespace-pre-wrap break-words font-mono leading-relaxed bg-surface border border-border p-2 max-h-80 overflow-y-auto">
          {bulletin.content}
        </pre>
      </section>

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">
          claims ({bulletin.claims.length})
        </h2>
        {bulletin.claims.length === 0 ? (
          <p className="text-[10px] text-muted/50">no claims extracted</p>
        ) : (
          <div className="flex flex-col gap-1">
            {bulletin.claims.map((c) => (
              <Link
                key={c.id}
                to="/memory"
                search={{ entity: c.subject_id }}
                className="bg-surface border border-border px-2 py-1.5 hover:border-accent/30 transition-colors"
              >
                <div className="flex items-center gap-2">
                  <span className={`text-[10px] px-1.5 py-0.5 rounded ${CLAIM_COLORS[c.claim_type_key] ?? "bg-gray-900/40 text-gray-300"}`}>
                    {c.claim_type_key}
                  </span>
                  <span className="text-xs text-text flex-1 truncate">
                    {c.subject_id} → {c.object_id || c.value || ""}
                  </span>
                  <span className={`text-[8px] px-1 rounded ${c.status === "active" ? "text-success/70 bg-success/10" : "text-muted/50 bg-muted/10"}`}>
                    {c.status}
                  </span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

export const Route = createFileRoute("/memory/bulletins/$bulletinId")({ component: BulletinDetailPage });
