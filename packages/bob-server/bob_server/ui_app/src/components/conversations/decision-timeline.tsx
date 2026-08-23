import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { fetchAPI, postAPI } from "@/lib/api";

interface TimelineItem {
  type: "attention" | "probe" | "turn" | "effect" | "goal";
  at: string;
  [key: string]: unknown;
}

interface BindingRow {
  session_key: string;
  channel: string;
  kind: string;
  address: string | null;
  merged_from: string | null;
  merged_at: string;
  created_at: string;
}

const TYPE_STYLE: Record<string, string> = {
  attention: "text-muted",
  probe: "text-accent",
  turn: "text-text",
  effect: "text-success",
  goal: "text-yellow-400",
};

function fmtTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function itemLabel(i: TimelineItem): string {
  switch (i.type) {
    case "attention":
      return `${i.decision}${i.addressed ? " · addressed" : ""}${i.reason ? ` · ${i.reason}` : ""}`;
    case "probe":
      return `${i.decision ?? "?"} — ${i.reason ?? ""}`;
    case "turn":
      return `turn ${String(i.status)}${i.error ? ` · ${i.error}` : ""}`;
    case "effect":
      return `${i.kind} ${i.status}${i.error ? ` · ${i.error}` : ""}`;
    case "goal":
      return `${i.from_status ?? "·"}→${i.to_status}: ${i.objective ?? ""}`;
    default:
      return "";
  }
}

export function DecisionTimeline({ conversationId }: { conversationId: string }) {
  const [open, setOpen] = useState(false);
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const { data } = useQuery<{ items: TimelineItem[] }>({
    queryKey: ["conversation-timeline", conversationId],
    queryFn: () =>
      fetchAPI<{ items: TimelineItem[] }>(
        `/conversations/${encodeURIComponent(conversationId)}/timeline`,
      ),
    enabled: open,
  });

  const items = data?.items ?? [];
  const shown = typeFilter === "all" ? items : items.filter((i) => i.type === typeFilter);
  const types = ["all", "attention", "probe", "turn", "effect", "goal"];

  return (
    <section>
      <button
        onClick={() => setOpen(!open)}
        className="text-xs text-muted font-sans uppercase tracking-wider mb-1 hover:text-text"
      >
        decisions & activity {open ? "▾" : "▸"}
      </button>
      {open && (
        <>
          <div className="flex gap-1 mb-1.5 overflow-x-auto">
            {types.map((t) => (
              <button
                key={t}
                onClick={() => setTypeFilter(t)}
                className={`px-1.5 py-0.5 text-[10px] border border-border shrink-0 ${
                  typeFilter === t ? "bg-accent text-bg" : "text-muted hover:text-text"
                }`}
              >
                {t}
              </button>
            ))}
          </div>
          <div className="bg-surface border border-border divide-y divide-border max-h-96 overflow-y-auto">
            {shown.length === 0 && (
              <div className="p-2 text-[11px] text-muted text-center">no activity</div>
            )}
            {shown.map((i, idx) => (
              <div key={idx} className="flex items-start gap-2 px-2 py-1 text-[11px]">
                <span className="text-muted shrink-0 tabular-nums">{fmtTs(i.at)}</span>
                <span className={`shrink-0 uppercase text-[9px] mt-0.5 ${TYPE_STYLE[i.type]}`}>
                  {i.type}
                </span>
                <span className="text-text min-w-0 break-words">{itemLabel(i)}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

export function BindingsCard({ conversationId }: { conversationId: string }) {
  const qc = useQueryClient();
  const { data } = useQuery<{ bindings: BindingRow[] }>({
    queryKey: ["conversation-bindings", conversationId],
    queryFn: () =>
      fetchAPI<{ bindings: BindingRow[] }>(
        `/conversations/${encodeURIComponent(conversationId)}/bindings`,
      ),
  });
  const unmerge = useMutation({
    mutationFn: (sessionKey: string) =>
      postAPI(`/bindings/${encodeURIComponent(sessionKey)}/unmerge`, {}),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["conversation-bindings", conversationId] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
    },
  });

  const bindings = data?.bindings ?? [];
  if (bindings.length <= 1 && !bindings.some((b) => b.merged_from)) return null;

  return (
    <section>
      <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-1">
        bindings ({bindings.length})
      </h2>
      <div className="bg-surface border border-border divide-y divide-border text-[11px]">
        {bindings.map((b) => (
          <div key={b.session_key} className="flex items-center gap-2 px-2 py-1">
            <span className="uppercase text-[9px] text-accent shrink-0">{b.channel}</span>
            <span className="text-text truncate flex-1">{b.session_key}</span>
            {b.merged_from && (
              <>
                <span
                  className="text-[9px] px-1 border border-yellow-500/60 text-yellow-400 shrink-0"
                  title={`merged from ${b.merged_from} at ${b.merged_at}`}
                >
                  merged {b.merged_at ? fmtTs(b.merged_at) : ""}
                </span>
                <button
                  className="border border-border px-1.5 py-0.5 text-muted hover:bg-border shrink-0"
                  disabled={unmerge.isPending}
                  onClick={() => unmerge.mutate(b.session_key)}
                >
                  unmerge
                </button>
              </>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
