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

function ResultCard({ r }: { r: SearchResult }) {
  const pathParts = r.path.replace("memory/", "").replace(".md", "").split("/");
  const category = pathParts[1] || "";
  return (
    <div className="flex items-start gap-2 py-1.5">
      <span className="text-[9px] text-accent bg-accent/10 px-1 rounded shrink-0 mt-0.5">{category}</span>
      <div className="flex flex-col min-w-0">
        <span className="text-[11px] text-text">{r.title}</span>
        {r.relevance && <span className="text-[10px] text-muted">{r.relevance}</span>}
        <span className="text-[9px] text-muted/50 font-mono">{r.path}</span>
      </div>
    </div>
  );
}

function MemoryPage() {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");
  const queryClient = useQueryClient();

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

  return (
    <div className="flex flex-col h-full">
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
            searchMutation.data.results.map((r, i) => <ResultCard key={i} r={r} />)
          )}
        </div>
      )}

      {searchMutation.isError && (
        <div className="px-3 py-1 text-[10px] text-error border-b border-border">Search failed</div>
      )}

      <div className="flex items-center gap-1 px-3 py-1.5 border-b border-border shrink-0">
        <span className="text-xs text-muted">History</span>
        <span className="text-[10px] text-muted/60 ml-auto">{searches.length}</span>
      </div>

      <div className="flex-1 overflow-y-auto">
        {searches.length === 0 ? (
          <div className="p-4 text-muted text-center text-xs">no memory searches yet</div>
        ) : (
          searches.map((s) => (
            <div key={s.id} className="border-b border-border">
              <button
                onClick={() => setExpanded(expanded === s.id ? null : s.id)}
                className="w-full flex items-center gap-2 px-3 py-2 hover:bg-surface transition-colors text-left"
              >
                <div className="flex flex-col min-w-0 flex-1">
                  <span className="text-xs text-text truncate">{s.query}</span>
                  <div className="flex items-center gap-1.5">
                    <span className="text-[10px] text-muted">{s.result_count} result{s.result_count !== 1 ? "s" : ""}</span>
                    {s.latency_seconds != null && (
                      <span className="text-[10px] text-muted">{s.latency_seconds.toFixed(1)}s</span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  <RelativeTime iso={s.created_at} />
                  <span className={`text-muted text-xs transition-transform ${expanded === s.id ? "rotate-90" : ""}`}>&rsaquo;</span>
                </div>
              </button>

              {expanded === s.id && (
                <div className="px-3 pb-2">
                  {s.abstract && (
                    <p className="text-[10px] text-text mb-1.5">{s.abstract}</p>
                  )}
                  {s.results.length > 0 ? (
                    s.results.map((r, i) => <ResultCard key={i} r={r} />)
                  ) : (
                    <div className="text-[10px] text-muted">No results</div>
                  )}
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/memory/")({ component: MemoryPage });
