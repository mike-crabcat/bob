import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useWSConnected } from "@/hooks/use-live-data";
import { SessionList } from "@/components/home/session-list";
import { LLMChart } from "@/components/home/llm-chart";
import { BulletinCards } from "@/components/home/bulletin-cards";
import { fetchAPI } from "@/lib/api";

interface CostByCategory {
  category: string;
  cost: number;
  call_count: number;
  prompt_tokens: number;
  completion_tokens: number;
}

interface HomeSnapshot {
  active_sessions: SessionItem[];
  chart_buckets: ChartBucket[];
  chart_categories: string[];
  recent_bulletins: BulletinItem[];
  active_dispatches: DispatchItem[];
  entity_count: number;
  bulletin_count: number;
  cost_by_category: CostByCategory[];
  total_cost_24h: number;
}

export interface SessionItem {
  session_key: string;
  channel: string;
  call_count: number;
  completed: number;
  failed: number;
  avg_latency: number;
  last_activity: string;
}

export interface ChartBucket {
  interval_start: string;
  [category: string]: string | number;
}

export interface BulletinItem {
  id: string;
  channel_id: string;
  source_type: string;
  content: string;
  created_at: string;
}

export interface DispatchItem {
  id: string;
  notification_type: string;
  session_key: string;
  task_id: string | null;
  task_title: string | null;
  project_title: string | null;
  dispatched_at: string;
  tap_count: number;
}

function HomePage() {
  const connected = useWSConnected();

  const { data: home } = useQuery<HomeSnapshot>({
    queryKey: ["home"],
    queryFn: () => fetchAPI<HomeSnapshot>("/home"),
  });

  if (!home && !connected) {
    return <div className="p-4 text-muted text-center">connecting...</div>;
  }

  return (
    <div className="flex flex-col gap-4 p-3">
      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">llm calls · 24h</h2>
        <LLMChart buckets={home?.chart_buckets ?? []} categories={home?.chart_categories ?? []} />
      </section>

      {home && home.cost_by_category && home.cost_by_category.length > 0 && (
        <section>
          <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">
            estimated cost · 24h
            <span className="text-text ml-2">${home.total_cost_24h.toFixed(2)}</span>
          </h2>
          <div className="bg-surface border border-border p-2">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted text-[10px] uppercase">
                  <th className="text-left px-2 pb-1">category</th>
                  <th className="text-right px-2 pb-1">calls</th>
                  <th className="text-right px-2 pb-1">prompt</th>
                  <th className="text-right px-2 pb-1">completion</th>
                  <th className="text-right px-2 pb-1">cost</th>
                </tr>
              </thead>
              <tbody>
                {home.cost_by_category.map((c) => (
                  <tr key={c.category} className="border-t border-border">
                    <td className="py-0.5 px-2">{c.category.replace(/_/g, " ")}</td>
                    <td className="text-right tabular-nums px-2">{c.call_count}</td>
                    <td className="text-right tabular-nums px-2">{fmtTokens(c.prompt_tokens)}</td>
                    <td className="text-right tabular-nums px-2">{fmtTokens(c.completion_tokens)}</td>
                    <td className="text-right tabular-nums px-2">${c.cost.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">sessions</h2>
        <SessionList sessions={home?.active_sessions?.slice(0, 8) ?? []} />
      </section>

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">bulletins</h2>
        <BulletinCards bulletins={home?.recent_bulletins ?? []} />
      </section>

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">stats</h2>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <StatBox label="entities" value={home?.entity_count ?? 0} />
          <StatBox label="bulletins" value={home?.bulletin_count ?? 0} />
          <StatBox label="dispatches" value={home?.active_dispatches?.length ?? 0} />
          <StatBox label="sessions" value={home?.active_sessions?.length ?? 0} />
        </div>
      </section>
    </div>
  );
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

function StatBox({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-surface border border-border p-2">
      <div className="text-muted text-[10px] font-sans uppercase">{label}</div>
      <div className="text-text text-base font-medium">{value}</div>
    </div>
  );
}

export const Route = createFileRoute("/")({ component: HomePage });
