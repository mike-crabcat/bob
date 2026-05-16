import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchAPI } from "@/lib/api";

interface ContactDetail {
  id: string;
  name: string;
  phone_number: string;
  email: string | null;
  is_trusted: boolean;
  is_default: boolean;
  whatsapp_groups: string[];
  sessions: ContactSession[];
  created_at: string;
  updated_at: string;
}

interface ContactSession {
  session_key: string;
  channel: string;
  call_count: number;
  last_active: string;
}

const CHANNEL_COLORS: Record<string, string> = {
  whatsapp: "text-whatsapp",
  email: "text-email",
  voice: "text-voice",
  other: "text-muted",
};

function ContactDetailPage() {
  const { contactId } = Route.useParams();

  const { data: detail } = useQuery<ContactDetail>({
    queryKey: ["contact-detail", contactId],
    queryFn: () => fetchAPI<ContactDetail>(`/contacts/${contactId}`),
  });

  if (!detail) {
    return <div className="p-4 text-muted text-center text-xs">loading...</div>;
  }

  return (
    <div className="flex flex-col gap-3 p-3">
      <div>
        <Link to="/contacts" className="text-xs text-accent hover:underline">&larr; contacts</Link>
        <h1 className="text-sm font-medium mt-1">{detail.name}</h1>
        <div className="flex items-center gap-2 mt-1 text-[10px] text-muted">
          <span>{detail.phone_number}</span>
          {detail.email && <span>{detail.email}</span>}
        </div>
        <div className="flex items-center gap-2 mt-1">
          <span className={`w-1.5 h-1.5 rounded-full ${detail.is_trusted ? "bg-success" : "bg-muted"}`} />
          <span className="text-[10px] text-muted">{detail.is_trusted ? "trusted" : "untrusted"}</span>
          {detail.is_default && <span className="text-[10px] text-accent">default</span>}
        </div>
      </div>

      {detail.sessions.length > 0 && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">sessions ({detail.sessions.length})</h2>
          {detail.sessions.map((s) => (
            <Link
              key={s.session_key}
              to="/sessions/$sessionKey"
              params={{ sessionKey: s.session_key }}
              className="flex items-center gap-2 px-2 py-1.5 border-b border-border hover:bg-surface transition-colors"
            >
              <span className={`text-[10px] uppercase shrink-0 ${CHANNEL_COLORS[s.channel] ?? "text-muted"}`}>
                {s.channel}
              </span>
              <span className="text-xs text-text truncate flex-1">
                {s.session_key.split(":").slice(-2).join(":")}
              </span>
              <span className="text-[10px] text-muted shrink-0">{s.call_count} calls</span>
            </Link>
          ))}
        </section>
      )}

      {detail.whatsapp_groups.length > 0 && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">whatsapp groups</h2>
          {detail.whatsapp_groups.map((g) => (
            <div key={g} className="text-xs text-text py-0.5">{g}</div>
          ))}
        </section>
      )}
    </div>
  );
}

export const Route = createFileRoute("/contacts/$contactId")({ component: ContactDetailPage });
