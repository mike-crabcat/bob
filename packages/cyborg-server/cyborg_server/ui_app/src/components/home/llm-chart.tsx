import { BarChart, Bar, XAxis, YAxis, Tooltip, Legend } from "recharts";
import { useRef, useState, useEffect } from "react";
import type { ChartBucket } from "@/routes";

const CATEGORY_COLORS: Record<string, string> = {
  whatsapp_incoming: "#22c55e",
  email_incoming: "#3b82f6",
  session_summary: "#d4a574",
  voice: "#a855f7",
  other: "#71717a",
};

interface Props {
  buckets: ChartBucket[];
  categories: string[];
}

function categoryColor(cat: string): string {
  return CATEGORY_COLORS[cat] ?? "#71717a";
}

function categoryLabel(cat: string): string {
  return cat.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function LLMChart({ buckets, categories }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      setW(Math.floor(entry.contentRect.width));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  if (buckets.length === 0) {
    return <div className="text-xs text-muted text-center py-3">no data</div>;
  }

  const data = buckets.map((b) => ({
    time: b.interval_start?.slice(11, 16) ?? "",
    ...Object.fromEntries(categories.map((c) => [c, (b[c] as number) ?? 0])),
  }));

  return (
    <div ref={ref} className="bg-surface border border-border p-2">
      {w > 0 && (
        <BarChart width={w} height={130} data={data} margin={{ top: 0, right: 0, left: -20, bottom: 0 }}>
          <XAxis
            dataKey="time"
            tick={{ fontSize: 9, fill: "#71717a" }}
            axisLine={{ stroke: "#2a2a2a" }}
            tickLine={false}
            interval={Math.max(0, Math.floor(data.length / 5) - 1)}
          />
          <YAxis
            tick={{ fontSize: 9, fill: "#71717a" }}
            axisLine={false}
            tickLine={false}
            width={25}
            allowDecimals={false}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "#222222",
              border: "1px solid #2a2a2a",
              fontSize: 10,
              fontFamily: "JetBrains Mono, monospace",
              color: "#e4e4e7",
            }}
            labelStyle={{ color: "#71717a" }}
          />
          <Legend
            wrapperStyle={{ fontSize: 9, fontFamily: "JetBrains Mono, monospace", paddingTop: 4 }}
            formatter={(value: string) => (
              <span style={{ color: "#71717a" }}>{categoryLabel(value)}</span>
            )}
          />
          {categories.map((cat) => (
            <Bar key={cat} dataKey={cat} stackId="calls" fill={categoryColor(cat)} radius={0} />
          ))}
        </BarChart>
      )}
    </div>
  );
}
