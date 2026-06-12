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

interface EntityDocument {
  entity_id: string;
  entity_type: string;
  display_name: string;
  status: string;
  rendered: string;
}

interface Claim {
  id: string;
  claim_type_key: string;
  subject_id: string;
  object_id: string | null;
  value: string | null;
  status: string;
  source_bulletins: string[];
  visibility: string;
  created_at: string | null;
}

const CHANNEL_COLORS: Record<string, string> = {
  whatsapp: "text-whatsapp",
  email: "text-email",
  voice: "text-voice",
  other: "text-muted",
};

const CLAIM_COLORS: Record<string, string> = {
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
  alias: "bg-gray-900/40 text-gray-300",
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
};

function ContactDetailPage() {
  const { contactId } = Route.useParams();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [editPhone, setEditPhone] = useState("");
  const [editEmail, setEditEmail] = useState("");
  const [editTrusted, setEditTrusted] = useState(false);
  const [expandedClaim, setExpandedClaim] = useState<string | null>(null);

  const { data: detail } = useQuery<ContactDetail>({
    queryKey: ["contact-detail", contactId],
    queryFn: () => fetchAPI<ContactDetail>(`/contacts/${contactId}`),
  });

  const { data: entity } = useQuery<EntityDocument | undefined>({
    queryKey: ["contact-entity", contactId],
    queryFn: async () => {
      const res = await fetchAPI<EntityDocument | { error: string }>(`/contacts/${contactId}/entity`);
      if (res && "error" in res) return undefined;
      return res as EntityDocument;
    },
    retry: false,
  });

  const { data: claims } = useQuery<Claim[] | undefined>({
    queryKey: ["contact-claims", contactId],
    queryFn: async () => {
      const res = await fetchAPI<Claim[] | { error: string }>(`/contacts/${contactId}/claims`);
      if (res && "error" in res) return undefined;
      return res as Claim[];
    },
    retry: false,
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

      {entity && (
        <section>
          <div className="flex items-center justify-between mb-1">
            <h2 className="text-xs text-muted font-sans uppercase tracking-wider">person entity</h2>
            <Link
              to="/memory"
              search={{ entity: entity.entity_id }}
              className="text-[10px] text-accent hover:underline"
            >
              view in memory →
            </Link>
          </div>
          <div className="text-xs text-text bg-surface border border-border p-2 whitespace-pre-wrap font-mono leading-relaxed max-h-96 overflow-y-auto">
            {entity.rendered || `${entity.entity_type}: ${entity.display_name}`}
          </div>
        </section>
      )}

      {claims && claims.length > 0 && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">claims ({claims.length})</h2>
          <div className="flex flex-col gap-1">
            {claims.map((c) => (
              <div
                key={c.id}
                onClick={() => setExpandedClaim(expandedClaim === c.id ? null : c.id)}
                className="bg-surface border border-border px-2 py-1.5 cursor-pointer hover:border-accent/30 transition-colors"
              >
                <div className="flex items-center gap-2">
                  <span className={`text-[10px] px-1.5 py-0.5 rounded ${CLAIM_COLORS[c.claim_type_key] ?? "bg-gray-900/40 text-gray-300"}`}>
                    {c.claim_type_key}
                  </span>
                  <span className="text-xs text-text flex-1 truncate">
                    {c.object_id ? `${c.subject_id} → ${c.object_id}` : c.value ? `${c.subject_id} → ${c.value}` : c.subject_id}
                  </span>
                  {c.created_at && (
                    <span className="text-[10px] text-muted shrink-0">
                      {new Date(c.created_at).toLocaleDateString()}
                    </span>
                  )}
                </div>
                {expandedClaim === c.id && (
                  <div className="mt-1 text-[10px] text-muted border-t border-border/30 pt-1 flex flex-col gap-0.5">
                    <div><span className="text-muted/50">from:</span> {c.subject_id}</div>
                    {c.object_id && <div><span className="text-muted/50">to:</span> {c.object_id}</div>}
                    {c.value && <div><span className="text-muted/50">value:</span> {c.value}</div>}
                    <div><span className="text-muted/50">vis:</span> {c.visibility}</div>
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

export const Route = createFileRoute("/contacts/$contactId")({ component: ContactDetailPage });
