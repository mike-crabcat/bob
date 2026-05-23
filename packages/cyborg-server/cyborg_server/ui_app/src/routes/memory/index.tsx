import { createFileRoute } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { fetchAPI } from "@/lib/api";

interface SearchResult {
  path: string;
  title: string;
  relevance: string;
}

interface SearchEntry {
  id: string;
  query: string;
  abstract: string;
  results: SearchResult[];
  session_key: string | null;
  result_count: number;
  latency_seconds: number | null;
  created_at: string | null;
}

interface SearchesSnapshot {
  searches: SearchEntry[];
}

interface SearchResponse {
  abstract: string;
  results: SearchResult[];
  latency_seconds: number;
}

interface MemoryEntry {
  path: string;
  wiki: string;
  category: string;
  slug: string;
  title: string;
  summary: string;
  modified: number;
}

interface MemoryStats {
  total_entries: number;
  wikis: Record<string, { entries: number; categories: Record<string, number> }>;
}

interface MemoryStatsResponse {
  stats: MemoryStats;
  recent: MemoryEntry[];
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

function RelativeTimeEpoch({ epoch }: { epoch: number }) {
  try {
    const diff = Date.now() - epoch * 1000;
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

function useMemoryContent(path: string | null) {
  return useQuery<{ content: string }>({
    queryKey: ["memory-content", path],
    queryFn: async () => {
      const secret = document.cookie.match(/cyborg_dashboard_secret=([^;]+)/)?.[1] ?? "";
      const base = import.meta.env.BASE_URL.replace(/\/$/, "");
      const res = await fetch(`${base}/api/workspace/file?path=${encodeURIComponent(path!)}&secret=${encodeURIComponent(secret)}`);
      if (!res.ok) throw new Error(`API ${res.status}`);
      return res.json();
    },
    enabled: path !== null,
  });
}

function MemoryContent({ path, onClose }: { path: string; onClose: () => void }) {
  const { data, isLoading, error } = useMemoryContent(path);
  return (
    <div className="border-b border-border">
      <div className="flex items-center gap-2 px-3 py-1 bg-surface/50 border-b border-border">
        <span className="text-[9px] text-accent bg-accent/10 px-1 rounded font-mono">{path}</span>
        <button onClick={onClose} className="ml-auto text-[10px] text-muted hover:text-text">close</button>
      </div>
      <div className="px-3 py-2">
        {isLoading && <span className="text-[10px] text-muted">Loading...</span>}
        {error && <span className="text-[10px] text-error">Failed to load</span>}
        {data?.content && (
          <pre className="text-[11px] text-text whitespace-pre-wrap break-words font-mono leading-relaxed">{data.content}</pre>
        )}
      </div>
    </div>
  );
}

function ResultCard({ r, onOpen }: { r: SearchResult; onOpen: (path: string) => void }) {
  const pathParts = r.path.replace("memory/", "").replace(".md", "").split("/");
  const category = pathParts[1] || "";
  return (
    <button onClick={() => onOpen(r.path)} className="w-full flex items-start gap-2 py-1.5 hover:bg-surface/50 transition-colors text-left">
      <span className="text-[9px] text-accent bg-accent/10 px-1 rounded shrink-0 mt-0.5">{category}</span>
      <div className="flex flex-col min-w-0">
        <span className="text-[11px] text-text">{r.title}</span>
        {r.relevance && <span className="text-[10px] text-muted">{r.relevance}</span>}
        <span className="text-[9px] text-muted/50 font-mono">{r.path}</span>
      </div>
    </button>
  );
}

function StatsBar({ stats }: { stats: MemoryStats }) {
  if (stats.total_entries === 0) return null;
  const categories: { name: string; count: number }[] = [];
  for (const wiki of Object.values(stats.wikis)) {
    for (const [cat, count] of Object.entries(wiki.categories)) {
      categories.push({ name: cat, count });
    }
  }
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0">
      <span className="text-xs text-text font-medium">{stats.total_entries}</span>
      <span className="text-[10px] text-muted">entries</span>
      <div className="flex items-center gap-1 ml-1">
        {categories.map((c) => (
          <span key={c.name} className="text-[9px] text-accent bg-accent/10 px-1.5 py-0.5 rounded">
            {c.name} <span className="text-muted">{c.count}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function RecentEntry({ entry, onOpen }: { entry: MemoryEntry; onOpen: (path: string) => void }) {
  return (
    <div className="border-b border-border">
      <button
        onClick={() => onOpen(entry.path)}
        className="w-full flex items-start gap-2 px-3 py-2 hover:bg-surface transition-colors text-left"
      >
        <div className="flex flex-col min-w-0 flex-1">
          <span className="text-[11px] text-text">{entry.title}</span>
          {entry.summary && (
            <span className="text-[10px] text-muted truncate">{entry.summary}</span>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0 mt-0.5">
          <span className="text-[9px] text-accent bg-accent/10 px-1 rounded">{entry.category}</span>
          <RelativeTimeEpoch epoch={entry.modified} />
        </div>
      </button>
    </div>
  );
}

function MemoryPage() {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");
  const [openPath, setOpenPath] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const { data: statsData } = useQuery<MemoryStatsResponse>({
    queryKey: ["memory-stats"],
    queryFn: () => fetchAPI<MemoryStatsResponse>("/memory/stats"),
  });

  const { data } = useQuery<SearchesSnapshot>({
    queryKey: ["memory-searches"],
    queryFn: () => fetchAPI<SearchesSnapshot>("/memory/searches"),
  });

  const searchMutation = useMutation({
    mutationFn: async (query: string): Promise<SearchResponse> => {
      const secret = document.cookie.match(/cyborg_dashboard_secret=([^;]+)/)?.[1] ?? "";
      const base = import.meta.env.BASE_URL.replace(/\/$/, "");
      const res = await fetch(`${base}/api/memory/search?q=${encodeURIComponent(query)}&secret=${encodeURIComponent(secret)}`);
      if (!res.ok) throw new Error(`API ${res.status}`);
      return res.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memory-searches"] });
    },
  });

  const handleSearch = () => {
    const q = searchInput.trim();
    if (q) searchMutation.mutate(q);
  };

  const searches = data?.searches ?? [];
  const recent = statsData?.recent ?? [];
  const stats = statsData?.stats ?? { total_entries: 0, wikis: {} };

  return (
    <div className="flex flex-col h-full">
      <StatsBar stats={stats} />

      <div className="flex gap-1 px-3 py-2 border-b border-border shrink-0">
        <input
          type="text"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          placeholder="Search memory..."
          className="flex-1 text-xs bg-transparent border border-border px-2 py-1 text-text placeholder:text-muted/50 focus:outline-none focus:border-accent"
        />
        <button
          onClick={handleSearch}
          disabled={!searchInput.trim() || searchMutation.isPending}
          className="px-2 py-1 text-[11px] border border-border text-muted hover:text-text hover:border-accent transition-colors disabled:opacity-30"
        >
          {searchMutation.isPending ? "..." : "Go"}
        </button>
      </div>

      {searchMutation.data && (
        <div className="px-3 py-2 border-b border-border bg-surface/50 shrink-0">
          <div className="flex items-center gap-1.5 mb-1">
            <span className="text-[10px] text-accent font-medium">
              {searchMutation.data.results.length} result{searchMutation.data.results.length !== 1 ? "s" : ""}
            </span>
            <span className="text-[10px] text-muted">{searchMutation.data.latency_seconds.toFixed(1)}s</span>
          </div>
          {searchMutation.data.abstract && (
            <p className="text-[11px] text-text mb-1.5">{searchMutation.data.abstract}</p>
          )}
          {searchMutation.data.results.length === 0 ? (
            <div className="text-[10px] text-muted py-1">No matches found</div>
          ) : (
            searchMutation.data.results.map((r, i) => <ResultCard key={i} r={r} onOpen={setOpenPath} />)
          )}
        </div>
      )}

      {searchMutation.isError && (
        <div className="px-3 py-1 text-[10px] text-error border-b border-border">Search failed</div>
      )}

      {openPath && (
        <MemoryContent path={openPath} onClose={() => setOpenPath(null)} />
      )}

      <div className="flex items-center gap-1 px-3 py-1.5 border-b border-border shrink-0">
        <span className="text-xs text-muted">Recent</span>
        <span className="text-[10px] text-muted/60 ml-auto">{recent.length}</span>
      </div>

      <div className="flex-1 overflow-y-auto">
        {recent.length === 0 ? (
          <div className="p-4 text-muted text-center text-xs">no memory entries yet</div>
        ) : (
          recent.map((entry) => <RecentEntry key={entry.path} entry={entry} onOpen={setOpenPath} />)
        )}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/memory/")({ component: MemoryPage });
