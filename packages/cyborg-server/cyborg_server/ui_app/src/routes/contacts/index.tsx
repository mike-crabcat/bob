import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { fetchAPI } from "@/lib/api";

interface ContactItem {
  id: string;
  name: string;
  phone_number: string;
  email: string | null;
  is_trusted: boolean;
  is_default: boolean;
  session_count: number;
  last_active: string | null;
  created_at: string;
  updated_at: string;
}

interface ContactsSnapshot {
  contacts: ContactItem[];
}

function RelativeTime({ iso }: { iso: string | null }) {
  if (!iso) return <span className="text-[10px] text-muted">--</span>;
  try {
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return <span className="text-[10px] text-success">now</span>;
    if (mins < 60) return <span className="text-[10px] text-muted">{mins}m</span>;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return <span className="text-[10px] text-muted">{hours}h</span>;
    return <span className="text-[10px] text-muted">{Math.floor(hours / 24)}d</span>;
  } catch {
    return <span className="text-[10px] text-muted">--</span>;
  }
}

function ContactsPage() {
  const [filter, setFilter] = useState<string>("all");

  const { data } = useQuery<ContactsSnapshot>({
    queryKey: ["contacts"],
    queryFn: () => fetchAPI<ContactsSnapshot>("/contacts"),
  });

  const contacts = data?.contacts ?? [];
  const filtered = filter === "all" ? contacts : contacts.filter((c) => c.is_trusted);

  return (
    <div className="flex flex-col h-full">
      <div className="flex gap-1 px-3 py-2 border-b border-border shrink-0">
        {["all", "trusted"].map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-2 py-1 text-[11px] border border-border shrink-0 transition-colors ${
              filter === f ? "bg-accent text-bg" : "text-muted hover:text-text"
            }`}
          >
            {f}
          </button>
        ))}
        <span className="text-[10px] text-muted self-center ml-auto">{filtered.length} contacts</span>
      </div>

      <div className="flex-1 overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="p-4 text-muted text-center text-xs">no contacts</div>
        ) : (
          filtered.map((c) => (
            <Link
              key={c.id}
              to="/contacts/$contactId"
              params={{ contactId: c.id }}
              className="flex items-center gap-2 px-3 py-2 border-b border-border hover:bg-surface transition-colors"
            >
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${c.is_trusted ? "bg-success" : "bg-muted"}`} />
              <div className="flex flex-col min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="text-xs text-text truncate">{c.name}</span>
                  {c.is_default && <span className="text-[9px] text-accent">default</span>}
                </div>
                <span className="text-[10px] text-muted">{c.phone_number}</span>
              </div>
              <div className="flex items-center gap-1.5 shrink-0">
                {c.session_count > 0 && <span className="text-[10px] text-muted">{c.session_count} sessions</span>}
                <RelativeTime iso={c.last_active} />
                <span className="text-muted text-xs">&rsaquo;</span>
              </div>
            </Link>
          ))
        )}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/contacts/")({ component: ContactsPage });
