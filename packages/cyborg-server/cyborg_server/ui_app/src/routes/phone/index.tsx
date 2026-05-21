import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { fetchAPI, postAPI } from "@/lib/api";

interface PhoneCall {
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

interface Contact {
  id: string;
  name: string;
  phone_number: string;
}

function relativeTime(ts: string): string {
  if (!ts) return "";
  const d = new Date(ts.endsWith("Z") ? ts : ts + "Z");
  const now = Date.now();
  const diff = now - d.getTime();
  if (diff < 60000) return "now";
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
  return `${Math.floor(diff / 86400000)}d ago`;
}

function dateGroup(ts: string): string {
  if (!ts) return "older";
  const d = new Date(ts.endsWith("Z") ? ts : ts + "Z");
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today.getTime() - 86400000);
  const callDate = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  if (callDate.getTime() === today.getTime()) return "Today";
  if (callDate.getTime() === yesterday.getTime()) return "Yesterday";
  return d.toLocaleDateString();
}

function formatDuration(seconds: number | null): string {
  if (seconds == null) return "";
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
    <span className={`text-[9px] px-1 py-0.5 ${colors[status] || "bg-surface text-muted"}`}>
      {status}
    </span>
  );
}

function PhoneListPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [showForm, setShowForm] = useState(false);
  const [toNumber, setToNumber] = useState("");
  const [agenda, setAgenda] = useState("");
  const [contactSearch, setContactSearch] = useState("");
  const [useContact, setUseContact] = useState(false);

  const { data: callsData } = useQuery<{ calls: PhoneCall[] }>({
    queryKey: ["phone-calls"],
    queryFn: () => fetchAPI<{ calls: PhoneCall[] }>("/phone/calls"),
    refetchInterval: 10000,
  });

  const { data: contactsData } = useQuery<{ contacts: Contact[] }>({
    queryKey: ["contacts"],
    queryFn: () => fetchAPI<{ contacts: Contact[] }>("/contacts"),
    enabled: showForm,
  });

  const callMutation = useMutation({
    mutationFn: (body: { to: string; agenda: string }) =>
      postAPI<{ call_id: string; call_sid: string; status: string }>("/phone/call", body),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["phone-calls"] });
      setShowForm(false);
      setToNumber("");
      setAgenda("");
      setContactSearch("");
      if (result.call_id) {
        navigate({ to: "/phone/$callId", params: { callId: result.call_id } });
      }
    },
  });

  const calls = callsData?.calls || [];
  const activeCalls = calls.filter((c) => c.status === "ringing" || c.status === "active");
  const pastCalls = calls.filter((c) => c.status !== "ringing" && c.status !== "active");

  const filteredContacts = (contactsData?.contacts || []).filter(
    (c) =>
      c.phone_number &&
      (contactSearch
        ? c.name.toLowerCase().includes(contactSearch.toLowerCase()) ||
          c.phone_number.includes(contactSearch)
        : true),
  );

  const handleSubmit = () => {
    const to = useContact ? toNumber : toNumber.trim();
    if (!to) return;
    callMutation.mutate({ to, agenda: agenda.trim() });
  };

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex items-center justify-between">
        <h1 className="text-sm font-medium">Phone</h1>
        <button
          onClick={() => setShowForm(!showForm)}
          className="text-[10px] bg-accent text-bg px-3 py-1 hover:opacity-90"
        >
          {showForm ? "cancel" : "new call"}
        </button>
      </div>

      {showForm && (
        <div className="bg-surface border border-border p-3 flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <button
              onClick={() => setUseContact(false)}
              className={`text-[10px] px-2 py-0.5 ${!useContact ? "bg-accent text-bg" : "text-muted border border-border"}`}
            >
              number
            </button>
            <button
              onClick={() => setUseContact(true)}
              className={`text-[10px] px-2 py-0.5 ${useContact ? "bg-accent text-bg" : "text-muted border border-border"}`}
            >
              contact
            </button>
          </div>

          {useContact ? (
            <div className="flex flex-col gap-1">
              <input
                type="text"
                placeholder="search contacts..."
                value={contactSearch}
                onChange={(e) => setContactSearch(e.target.value)}
                className="bg-bg border border-border text-xs text-text px-2 py-1"
              />
              <div className="max-h-[120px] overflow-y-auto border border-border">
                {filteredContacts.map((c) => (
                  <button
                    key={c.id}
                    onClick={() => { setToNumber(c.phone_number); setContactSearch(c.name); }}
                    className={`w-full text-left text-xs px-2 py-1 hover:bg-accent/10 ${toNumber === c.phone_number ? "bg-accent/10 text-accent" : "text-text"}`}
                  >
                    {c.name} <span className="text-muted">{c.phone_number}</span>
                  </button>
                ))}
                {filteredContacts.length === 0 && (
                  <div className="text-xs text-muted px-2 py-1">no contacts found</div>
                )}
              </div>
            </div>
          ) : (
            <input
              type="tel"
              placeholder="+61400123456"
              value={toNumber}
              onChange={(e) => setToNumber(e.target.value)}
              className="bg-bg border border-border text-xs text-text px-2 py-1"
            />
          )}

          <textarea
            placeholder="Call agenda..."
            value={agenda}
            onChange={(e) => setAgenda(e.target.value)}
            className="bg-bg border border-border text-xs text-text px-2 py-1 min-h-[60px] resize-y"
            rows={3}
          />

          <div className="flex items-center gap-2">
            <button
              onClick={handleSubmit}
              disabled={!toNumber.trim() || callMutation.isPending}
              className="text-[10px] bg-accent text-bg px-3 py-1 hover:opacity-90 disabled:opacity-50"
            >
              {callMutation.isPending ? "calling..." : "call"}
            </button>
            {callMutation.isError && (
              <span className="text-[10px] text-error">call failed</span>
            )}
          </div>
        </div>
      )}

      {activeCalls.length > 0 && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">active</h2>
          {activeCalls.map((call) => (
            <Link
              key={call.id}
              to="/phone/$callId"
              params={{ callId: call.id }}
              className="flex items-center gap-2 border border-green-500/30 bg-green-500/5 px-2 py-2 mb-px hover:bg-green-500/10 transition-colors"
            >
              <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse shrink-0" />
              <span className="text-xs font-medium text-text">
                {call.contact_name || call.phone_number}
              </span>
              <StatusBadge status={call.status} />
              {call.exchange_count > 0 && (
                <span className="text-[10px] text-muted">{call.exchange_count} exchanges</span>
              )}
              {call.agenda && (
                <span className="text-[10px] text-muted truncate ml-auto max-w-[120px]">
                  {call.agenda}
                </span>
              )}
              <span className="text-xs text-muted ml-auto">&rsaquo;</span>
            </Link>
          ))}
        </section>
      )}

      {pastCalls.length > 0 ? (
        <section>
          {(() => {
            const groups: Record<string, PhoneCall[]> = {};
            for (const call of pastCalls) {
              const g = dateGroup(call.started_at);
              (groups[g] ??= []).push(call);
            }
            return Object.entries(groups).map(([label, groupCalls]) => (
              <div key={label} className="mb-2">
                <h2 className="text-[10px] text-muted font-sans uppercase tracking-wider mb-1">
                  {label}
                </h2>
                {groupCalls.map((call) => (
                  <Link
                    key={call.id}
                    to="/phone/$callId"
                    params={{ callId: call.id }}
                    className="flex items-center gap-2 border-b border-border py-2 hover:bg-surface transition-colors"
                  >
                    <span className="text-xs text-muted shrink-0">
                      {call.direction === "outbound" ? "↗" : "↙"}
                    </span>
                    <div className="flex flex-col min-w-0 flex-1">
                      <span className="text-xs text-text truncate">
                        {call.contact_name || call.phone_number}
                      </span>
                      {call.agenda && (
                        <span className="text-[10px] text-muted truncate">{call.agenda}</span>
                      )}
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      {call.exchange_count > 0 && (
                        <span className="text-[10px] text-muted">{call.exchange_count}x</span>
                      )}
                      {call.duration_seconds != null && (
                        <span className="text-[10px] text-muted">
                          {formatDuration(call.duration_seconds)}
                        </span>
                      )}
                      <StatusBadge status={call.status} />
                      <span className="text-[10px] text-muted">
                        {relativeTime(call.started_at)}
                      </span>
                    </div>
                  </Link>
                ))}
              </div>
            ));
          })()}
        </section>
      ) : (
        !activeCalls.length && (
          <div className="text-xs text-muted text-center py-8">
            no phone calls yet
          </div>
        )
      )}
    </div>
  );
}

export const Route = createFileRoute("/phone/")({ component: PhoneListPage });
