import { createFileRoute } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { fetchAPI, postAPI, patchAPI } from "@/lib/api";

interface PersonaConfig {
  owner_name: string;
  model: string;
  channel: string;
  host: string;
}

interface PersonaRecord {
  id: string;
  revision: number;
  soul: string;
  identity: string;
  agents: string;
  user_content: string;
  config: PersonaConfig;
  is_active: boolean;
  created_at: string;
}

interface PersonaResponse {
  data: PersonaRecord;
}

interface HistoryResponse {
  data: PersonaRecord[];
}

function RelativeTime({ iso }: { iso: string }) {
  if (!iso) return <span className="text-[10px] text-muted">--</span>;
  try {
    const d = new Date(iso);
    const diff = Date.now() - d.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return <span className="text-[10px] text-success">now</span>;
    if (mins < 60) return <span className="text-[10px] text-muted">{mins}m ago</span>;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return <span className="text-[10px] text-muted">{hours}h ago</span>;
    return <span className="text-[10px] text-muted">{Math.floor(hours / 24)}d ago</span>;
  } catch {
    return <span className="text-[10px] text-muted">--</span>;
  }
}

const SECTION_HEADERS: Record<string, string> = {
  SOUL: "Your Soul — defines who you are at your core: personality, communication style, and behavioral rules.",
  IDENTITY: "Your Identity — who you are, what you do, and how you work. Placeholders like {owner_name}, {model}, {channel}, {host} are filled from config.",
  AGENTS: "Your Agents — behavioral guardrails for tool use, group chats, and external actions.",
  USER: "Your User — everything you know about the person you're helping.",
};

function CollapsibleSection({
  title,
  value,
  onChange,
  disabled,
}: {
  title: string;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border border-border rounded-lg overflow-hidden">
      <button
        type="button"
        className="w-full flex items-center justify-between px-3 py-2 bg-surface hover:bg-surface/80 text-sm font-medium text-text"
        onClick={() => setOpen(!open)}
      >
        <span>{title}</span>
        <span className="text-muted text-xs">{open ? "▲" : "▼"}</span>
      </button>
      {open && (
        <>
          <div className="px-3 py-1.5 border-t border-border bg-surface/50 text-[10px] text-muted italic">
            {SECTION_HEADERS[title] ?? ""}
          </div>
          <textarea
            className="w-full h-64 px-3 py-2 bg-bg text-text text-xs font-mono resize-y border-t border-border outline-none"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            disabled={disabled}
          />
        </>
      )}
    </div>
  );
}

function PersonaPage() {
  const qc = useQueryClient();
  const [viewingRevision, setViewingRevision] = useState<number | null>(null);

  const active = useQuery<PersonaResponse>({
    queryKey: ["persona"],
    queryFn: () => fetchAPI<PersonaResponse>("/persona"),
  });

  const history = useQuery<HistoryResponse>({
    queryKey: ["persona-history"],
    queryFn: () => fetchAPI<HistoryResponse>("/v1/persona/history"),
  });

  const viewedRecord = viewingRevision !== null
    ? history.data?.data.find((r) => r.revision === viewingRevision)
    : active.data?.data;

  const [form, setForm] = useState<{
    soul: string;
    identity: string;
    agents: string;
    user_content: string;
    config: PersonaConfig;
  } | null>(null);

  // Sync form when viewed record changes
  const currentForm = form ?? (viewedRecord
    ? {
        soul: viewedRecord.soul,
        identity: viewedRecord.identity,
        agents: viewedRecord.agents,
        user_content: viewedRecord.user_content,
        config: { ...viewedRecord.config },
      }
    : null);

  const isEditingActive = viewedRecord?.is_active && !viewingRevision;
  const hasChanges = currentForm && viewedRecord && (
    currentForm.soul !== viewedRecord.soul ||
    currentForm.identity !== viewedRecord.identity ||
    currentForm.agents !== viewedRecord.agents ||
    currentForm.user_content !== viewedRecord.user_content ||
    currentForm.config.owner_name !== viewedRecord.config.owner_name ||
    currentForm.config.model !== viewedRecord.config.model ||
    currentForm.config.channel !== viewedRecord.config.channel ||
    currentForm.config.host !== viewedRecord.config.host
  );

  const saveMutation = useMutation({
    mutationFn: (payload: typeof currentForm) =>
      postAPI<PersonaResponse>("/persona", payload!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["persona"] });
      qc.invalidateQueries({ queryKey: ["persona-history"] });
      setForm(null);
    },
  });

  const activateMutation = useMutation({
    mutationFn: (revision: number) =>
      patchAPI<PersonaResponse>(`/v1/persona/${revision}/activate`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["persona"] });
      qc.invalidateQueries({ queryKey: ["persona-history"] });
      setViewingRevision(null);
    },
  });

  if (active.isLoading) {
    return <div className="p-4 text-muted text-sm">Loading...</div>;
  }

  if (active.error || !active.data?.data) {
    return <div className="p-4 text-error text-sm">No persona record found</div>;
  }

  const records = history.data?.data ?? [];
  const update = (field: string, value: string) => {
    if (!currentForm) return;
    setForm({ ...currentForm, [field]: value });
  };
  const updateConfig = (field: keyof PersonaConfig, value: string) => {
    if (!currentForm) return;
    setForm({ ...currentForm, config: { ...currentForm.config, [field]: value } });
  };

  return (
    <div className="flex h-full">
      {/* Sidebar: revision history */}
      <div className="w-48 shrink-0 border-r border-border overflow-y-auto">
        <div className="p-3 text-xs font-medium text-muted uppercase tracking-wider">Revisions</div>
        {records.map((r) => (
          <button
            key={r.id}
            type="button"
            className={`w-full text-left px-3 py-2 text-xs flex items-center gap-2 hover:bg-surface/50 ${
              (viewingRevision ?? active.data!.data.revision) === r.revision
                ? "bg-surface border-l-2 border-accent"
                : ""
            }`}
            onClick={() => {
              setViewingRevision(r.revision === active.data!.data.revision ? null : r.revision);
              setForm(null);
            }}
          >
            <span className="font-mono text-text">r{r.revision}</span>
            {r.is_active && (
              <span className="bg-success/20 text-success px-1.5 py-0.5 rounded text-[10px]">active</span>
            )}
            <span className="ml-auto"><RelativeTime iso={r.created_at} /></span>
          </button>
        ))}
      </div>

      {/* Main: editor */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {/* Header */}
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold text-text">
            Persona r{viewedRecord?.revision ?? "?"}
          </h1>
          {viewedRecord?.is_active ? (
            <span className="bg-success/20 text-success text-[10px] px-2 py-0.5 rounded-full">ACTIVE</span>
          ) : viewedRecord && (
            <button
              type="button"
              className="bg-accent/20 text-accent text-[10px] px-2 py-0.5 rounded-full hover:bg-accent/30"
              onClick={() => activateMutation.mutate(viewedRecord.revision)}
              disabled={activateMutation.isPending}
            >
              {activateMutation.isPending ? "Activating..." : "Activate"}
            </button>
          )}
          <span className="text-[10px] text-muted"><RelativeTime iso={viewedRecord?.created_at ?? ""} /></span>
        </div>

        {/* Config fields */}
        {currentForm && (
          <div className="grid grid-cols-4 gap-2">
            <div>
              <label className="text-[10px] text-muted block mb-1">Owner</label>
              <input
                className="w-full px-2 py-1 bg-bg border border-border rounded text-xs text-text outline-none focus:border-accent"
                value={currentForm.config.owner_name}
                onChange={(e) => updateConfig("owner_name", e.target.value)}
                disabled={!isEditingActive}
              />
            </div>
            <div>
              <label className="text-[10px] text-muted block mb-1">Model</label>
              <input
                className="w-full px-2 py-1 bg-bg border border-border rounded text-xs text-text outline-none focus:border-accent"
                value={currentForm.config.model}
                onChange={(e) => updateConfig("model", e.target.value)}
                disabled={!isEditingActive}
              />
            </div>
            <div>
              <label className="text-[10px] text-muted block mb-1">Channel</label>
              <input
                className="w-full px-2 py-1 bg-bg border border-border rounded text-xs text-text outline-none focus:border-accent"
                value={currentForm.config.channel}
                onChange={(e) => updateConfig("channel", e.target.value)}
                disabled={!isEditingActive}
              />
            </div>
            <div>
              <label className="text-[10px] text-muted block mb-1">Host</label>
              <input
                className="w-full px-2 py-1 bg-bg border border-border rounded text-xs text-text outline-none focus:border-accent"
                value={currentForm.config.host}
                onChange={(e) => updateConfig("host", e.target.value)}
                disabled={!isEditingActive}
              />
            </div>
          </div>
        )}

        {/* Text sections */}
        {currentForm && (
          <>
            <CollapsibleSection
              title="SOUL"
              value={currentForm.soul}
              onChange={(v) => update("soul", v)}
              disabled={!isEditingActive}
            />
            <CollapsibleSection
              title="IDENTITY"
              value={currentForm.identity}
              onChange={(v) => update("identity", v)}
              disabled={!isEditingActive}
            />
            <CollapsibleSection
              title="AGENTS"
              value={currentForm.agents}
              onChange={(v) => update("agents", v)}
              disabled={!isEditingActive}
            />
            <CollapsibleSection
              title="USER"
              value={currentForm.user_content}
              onChange={(v) => update("user_content", v)}
              disabled={!isEditingActive}
            />
          </>
        )}

        {/* Save button */}
        {isEditingActive && hasChanges && (
          <div className="flex gap-2 pt-2">
            <button
              type="button"
              className="px-4 py-1.5 bg-accent text-bg text-xs font-medium rounded hover:bg-accent/90 disabled:opacity-50"
              onClick={() => saveMutation.mutate(currentForm!)}
              disabled={saveMutation.isPending}
            >
              {saveMutation.isPending ? "Saving..." : "Save as new revision"}
            </button>
            <button
              type="button"
              className="px-4 py-1.5 bg-surface text-muted text-xs rounded hover:bg-surface/80"
              onClick={() => setForm(null)}
            >
              Discard
            </button>
          </div>
        )}

        {saveMutation.isError && (
          <div className="text-error text-xs">Save failed: {saveMutation.error.message}</div>
        )}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/persona/")({
  component: PersonaPage,
});
