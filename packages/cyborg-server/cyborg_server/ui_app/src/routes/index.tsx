import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useWSConnected } from "@/hooks/use-live-data";
import { SessionList } from "@/components/home/session-list";
import { LLMChart } from "@/components/home/llm-chart";
import { SummaryCards } from "@/components/home/summary-cards";
import { fetchAPI } from "@/lib/api";

interface HomeSnapshot {
  active_sessions: SessionItem[];
  chart_buckets: ChartBucket[];
  chart_categories: string[];
  recent_summaries: SummaryItem[];
  active_dispatches: DispatchItem[];
  project_stats: Record<string, number>;
  task_stats: Record<string, number>;
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

export interface SummaryItem {
  id: string;
  session_key: string;
  summary_text: string;
  topics: string[];
  participants: string[];
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

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">sessions</h2>
        <SessionList sessions={home?.active_sessions?.slice(0, 8) ?? []} />
      </section>

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">summaries</h2>
        <SummaryCards summaries={home?.recent_summaries ?? []} />
      </section>

      <section>
        <h2 className="text-xs text-muted font-sans uppercase tracking-wider mb-2">stats</h2>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <StatBox label="projects" value={Object.values(home?.project_stats ?? {}).reduce((a: number, b: number) => a + b, 0)} />
          <StatBox label="active tasks" value={home?.task_stats?.active ?? 0} />
          <StatBox label="dispatches" value={home?.active_dispatches?.length ?? 0} />
          <StatBox label="sessions" value={home?.active_sessions?.length ?? 0} />
        </div>
      </section>
    </div>
  );
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
