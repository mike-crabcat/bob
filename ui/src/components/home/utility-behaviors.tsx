import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { fetchAPI } from "@/lib/api";

export interface UtilityRoute {
  id: number;
  source: string;
  type_pattern: string;
  level: string;
  enabled: boolean;
  hours: string | null;
  cooldown_s: number | null;
  budget_per_hour: number | null;
  note: string | null;
  fires_24h: number;
  last_fire_at: string | null;
}

export interface UtilityBehavior {
  session_key: string;
  title: string;
  enabled: boolean;
  model_alias: string;
  report_to: string | null;
  charter: string;
  created_by: string;
  last_turn_at: string | null;
  routes: UtilityRoute[];
}

interface BehaviorsSnapshot {
  behaviors: UtilityBehavior[];
}

function relTime(iso: string | null): string {
  if (!iso) return "never";
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 1440)}d ago`;
}

export function UtilityBehaviors() {
  const [open, setOpen] = useState(false);
  const { data } = useQuery<BehaviorsSnapshot>({
    queryKey: ["utility-behaviors"],
    queryFn: () => fetchAPI<BehaviorsSnapshot>("/utility_behaviors"),
    refetchInterval: 30_000,
  });
  if (!data?.behaviors?.length) return null;

  const behaviors = data.behaviors;
  const dayAgo = Date.now() - 86_400_000;
  const turned24h = behaviors.filter(
    (b) => b.last_turn_at && new Date(b.last_turn_at).getTime() > dayAgo,
  ).length;
  const disabled = behaviors.filter((b) => !b.enabled).length;
  const fires24h = behaviors.reduce(
    (a, b) => a + b.routes.reduce((x, r) => x + r.fires_24h, 0),
    0,
  );
  const lastTurnIso = behaviors.reduce<string | null>(
    (acc, b) =>
      b.last_turn_at && (!acc || b.last_turn_at > acc) ? b.last_turn_at : acc,
    null,
  );

  return (
    <section>
      <div className="bg-surface border border-border text-xs">
        <div className="px-2 py-1 flex items-center gap-3 flex-wrap">
          <span className="text-[10px] font-sans uppercase text-muted tracking-wide">
            utility behaviors
          </span>
          <span className="tabular-nums">{behaviors.length}</span>
          <span className="text-muted">
            {turned24h} turned 24h
            {disabled > 0 && (
              <span className="text-red-400"> · {disabled} off</span>
            )}
          </span>
          <span className="text-muted tabular-nums">{fires24h} route fires</span>
          <span className="text-muted ml-auto">
            {lastTurnIso ? `last turn ${relTime(lastTurnIso)}` : "no turns yet"}
          </span>
          <button
            className="border border-border px-2 py-0.5 hover:bg-border shrink-0"
            onClick={() => setOpen((v) => !v)}
          >
            {open ? "hide" : "details"}
          </button>
        </div>
        {open && (
          <div className="divide-y divide-border border-t border-border">
            {behaviors.map((b) => (
              <div key={b.session_key} className="p-2">
                <div className="flex items-center gap-2">
                  <span
                    className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                      b.enabled ? "bg-success" : "bg-red-500"
                    }`}
                    title={b.enabled ? "enabled" : "disabled"}
                  />
                  <Link
                    to="/conversations/$sessionKey"
                    params={{ sessionKey: b.session_key }}
                    className="text-text font-medium hover:underline truncate"
                    title={b.session_key}
                  >
                    {b.title}
                  </Link>
                  <span className="text-muted shrink-0">
                    {b.model_alias}
                    {b.report_to ? " → reports" : " · watch-only"}
                  </span>
                  <span className="text-muted ml-auto shrink-0">
                    {b.last_turn_at
                      ? `turned ${relTime(b.last_turn_at)}`
                      : "no turns yet"}
                  </span>
                </div>
                {b.routes.map((r) => (
                  <div
                    key={r.id}
                    className="flex items-center gap-2 mt-1 pl-3.5 flex-wrap"
                  >
                    <span
                      className={`tabular-nums ${r.enabled ? "text-text" : "text-red-400"}`}
                    >
                      {r.source} / {r.type_pattern} / {r.level}
                    </span>
                    <span className="text-muted">
                      {[
                        r.hours ?? "24h",
                        r.cooldown_s ? `${r.cooldown_s}s cd` : null,
                        r.budget_per_hour ? `${r.budget_per_hour}/h` : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                    <span className="text-muted ml-auto">
                      {r.fires_24h} fires/24h · last {relTime(r.last_fire_at)}
                    </span>
                  </div>
                ))}
                <div
                  className="text-muted mt-1 pl-3.5 line-clamp-2"
                  title={b.charter}
                >
                  {b.charter}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
