import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { fetchAPI, putAPI } from "@/lib/api";

interface ContactGroup {
  name: string;
  jid: string;
  is_admin: boolean;
  joined_at: string;
}

interface ContactDetail {
  id: string;
  name: string;
  phone_number: string;
  email: string | null;
  is_trusted: boolean;
  is_default: boolean;
  groups: ContactGroup[];
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
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [editPhone, setEditPhone] = useState("");
  const [editEmail, setEditEmail] = useState("");
  const [editTrusted, setEditTrusted] = useState(false);

  const { data: detail } = useQuery<ContactDetail>({
    queryKey: ["contact-detail", contactId],
    queryFn: () => fetchAPI<ContactDetail>(`/contacts/${contactId}`),
  });

  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      putAPI<{ ok: boolean }>(`/contacts/${contactId}`, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["contact-detail", contactId] });
      queryClient.invalidateQueries({ queryKey: ["contacts"] });
      setEditing(false);
    },
  });

  if (!detail) {
    return <div className="p-4 text-muted text-center text-xs">loading...</div>;
  }

  const startEdit = () => {
    setEditName(detail.name);
    setEditPhone(detail.phone_number);
    setEditEmail(detail.email ?? "");
    setEditTrusted(detail.is_trusted);
    setEditing(true);
  };

  const save = () => {
    const body: Record<string, unknown> = {};
    if (editName !== detail.name) body.name = editName;
    if (editPhone !== detail.phone_number) body.phone_number = editPhone;
    if (editEmail !== (detail.email ?? "")) body.email = editEmail || null;
    if (editTrusted !== detail.is_trusted) body.is_trusted = editTrusted;
    if (Object.keys(body).length === 0) {
      setEditing(false);
      return;
    }
    mutation.mutate(body);
  };

  return (
    <div className="flex flex-col gap-3 p-3">
      <div>
        <div className="flex items-center justify-between">
          <Link to="/contacts" className="text-xs text-accent hover:underline">&larr; contacts</Link>
          {!editing && (
            <button
              onClick={startEdit}
              className="text-[10px] text-accent hover:underline"
            >
              edit
            </button>
          )}
        </div>

        {editing ? (
          <div className="flex flex-col gap-2 mt-2">
            <label className="flex flex-col gap-0.5">
              <span className="text-[10px] text-muted uppercase">name</span>
              <input
                type="text"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                className="bg-surface border border-border text-xs text-text px-2 py-1"
              />
            </label>
            <label className="flex flex-col gap-0.5">
              <span className="text-[10px] text-muted uppercase">phone</span>
              <input
                type="text"
                value={editPhone}
                onChange={(e) => setEditPhone(e.target.value)}
                className="bg-surface border border-border text-xs text-text px-2 py-1"
              />
            </label>
            <label className="flex flex-col gap-0.5">
              <span className="text-[10px] text-muted uppercase">email</span>
              <input
                type="text"
                value={editEmail}
                onChange={(e) => setEditEmail(e.target.value)}
                placeholder="none"
                className="bg-surface border border-border text-xs text-text px-2 py-1 placeholder:text-muted/50"
              />
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={editTrusted}
                onChange={(e) => setEditTrusted(e.target.checked)}
                className="accent-accent"
              />
              <span className="text-xs text-text">trusted</span>
            </label>
            <div className="flex gap-2 mt-1">
              <button
                onClick={save}
                disabled={mutation.isPending}
                className="text-[10px] bg-accent text-bg px-3 py-1 hover:opacity-90 disabled:opacity-50"
              >
                {mutation.isPending ? "saving..." : "save"}
              </button>
              <button
                onClick={() => setEditing(false)}
                disabled={mutation.isPending}
                className="text-[10px] text-muted hover:text-text px-3 py-1"
              >
                cancel
              </button>
              {mutation.isError && (
                <span className="text-[10px] text-red-400 self-center">
                  save failed
                </span>
              )}
            </div>
          </div>
        ) : (
          <>
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
          </>
        )}
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

      {detail.groups.length > 0 && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">whatsapp groups ({detail.groups.length})</h2>
          {detail.groups.map((g) => (
            <div key={g.jid} className="flex items-center gap-2 py-0.5">
              <span className="text-xs text-text">{g.name || g.jid}</span>
              {g.is_admin && <span className="text-[10px] text-accent">admin</span>}
            </div>
          ))}
        </section>
      )}
    </div>
  );
}

export const Route = createFileRoute("/contacts/$contactId")({ component: ContactDetailPage });
