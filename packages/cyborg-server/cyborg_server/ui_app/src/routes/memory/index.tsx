import { createFileRoute } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useEffect } from "react";
import { fetchAPI } from "@/lib/api";

interface SearchResult {
  path: string;
  title: string;
  relevance: string;
}

interface SearchResponse {
  abstract: string;
  results: SearchResult[];
  latency_seconds: number;
}

interface MemoryStats {
  total_entries: number;
  wikis: Record<string, {
    entries: number;
    categories: Record<string, number>;
    internal_categories: Record<string, number>;
  }>;
}

interface MemoryStatsResponse {
  stats: MemoryStats;
  recent: { path: string; wiki: string; category: string; slug: string; title: string; summary: string; modified: number }[];
  pending_bulletins: number;
  last_dream: string | null;
}

interface Bulletin {
  slug: string;
  source_session: string;
  source_type: string;
  time_window: string;
  participants: string;
  contact_ids: string;
  intended_category: string;
  content: string;
  created_at: number;
}

interface DreamLog {
  id: string;
  bulletins_processed: number;
  entries_created: number;
  claims_extracted: number;
  bulletin_slugs: string[];
  operations: Array<{ bulletin: string; source: string; claims: number; entity_ops: number; content_preview: string }>;
  raw_response: string;
  duration_seconds: number | null;
  status: string;
  created_at: string;
}

function relativeTime(iso: string | null): string {
  if (!iso) return "--";
  try {
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "now";
    if (mins < 60) return `${mins}m`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h`;
    return `${Math.floor(hours / 24)}d`;
  } catch {
    return "--";
  }
}

function relativeTimeEpoch(epoch: number): string {
  const diff = Date.now() - epoch * 1000;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
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

function ContentViewer({ path, onClose }: { path: string; onClose: () => void }) {
  const { data, isLoading, error } = useMemoryContent(path);
  return (
    <div className="border-b border-border bg-surface/30">
      <div className="flex items-center gap-2 px-3 py-1 border-b border-border/50">
        <span className="text-[9px] text-accent bg-accent/10 px-1 rounded font-mono truncate">{path}</span>
        <button onClick={onClose} className="ml-auto text-[10px] text-muted hover:text-text">close</button>
      </div>
      <div className="px-3 py-2 max-h-60 overflow-y-auto">
        {isLoading && <span className="text-[10px] text-muted">Loading...</span>}
        {error && <span className="text-[10px] text-error">Failed to load</span>}
        {data?.content && (
          <pre className="text-[11px] text-text whitespace-pre-wrap break-words font-mono leading-relaxed">{data.content}</pre>
        )}
      </div>
    </div>
  );
}

function BulletinCard({ b, onOpen }: { b: Bulletin; onOpen: (path: string) => void }) {
  const firstLine = b.content.split("\n")[0].replace(/^-\s*/, "");
  const bulletinPath = `memory/core/bulletins/${b.slug}.md`;
  return (
    <div className="flex items-start gap-2 px-3 py-2 border-b border-border/50 hover:bg-surface/30 transition-colors">
      <span className="text-[8px] text-warning/80 bg-warning/10 px-1 rounded shrink-0 mt-0.5">queued</span>
      {b.intended_category && (
        <span className="text-[8px] text-accent/60 bg-accent/5 px-1 rounded shrink-0 mt-0.5">
          → {b.intended_category}
        </span>
      )}
      <button
        onClick={() => onOpen(bulletinPath)}
        className="flex flex-col min-w-0 flex-1 text-left"
      >
        <span className="text-[11px] text-text truncate">{firstLine.slice(0, 100)}</span>
        {b.participants && (
          <span className="text-[9px] text-muted/60">{b.participants}</span>
        )}
      </button>
      <span className="text-[9px] text-muted/50 shrink-0 mt-0.5">{relativeTimeEpoch(b.created_at)}</span>
    </div>
  );
}

function DreamRunCard({ d, onOpen, onRedigest }: { d: DreamLog; onOpen: (path: string) => void; onRedigest: (slug: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  const [bulletinContent, setBulletinContent] = useState<Record<string, string>>({});
  const [fetchedBulletins, setFetchedBulletins] = useState(false);

  const statusColor =
    d.status === "completed" ? "text-success bg-success/10" :
    d.status === "failed" ? "text-error bg-error/10" :
    "text-muted bg-muted/10";

  const fetchBulletins = async () => {
    if (fetchedBulletins || d.bulletin_slugs.length === 0) return;
    try {
      const secret = document.cookie.match(/cyborg_dashboard_secret=([^;]+)/)?.[1] ?? "";
      const base = import.meta.env.BASE_URL.replace(/\/$/, "");
      const res = await fetch(`${base}/api/memory/digested?secret=${encodeURIComponent(secret)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slugs: d.bulletin_slugs }),
      });
      if (!res.ok) return;
      const data = await res.json();
      const map: Record<string, string> = {};
      for (const b of data.bulletins ?? []) {
        map[b.slug] = b.content;
      }
      setBulletinContent(map);
      setFetchedBulletins(true);
    } catch { /* ignore */ }
  };

  const toggleExpand = () => {
    if (!expanded) fetchBulletins();
    setExpanded(!expanded);
  };

  return (
    <div className="border-b border-border">
      <button
        onClick={toggleExpand}
        className="w-full px-3 py-2 flex items-start gap-2 hover:bg-surface/30 transition-colors text-left"
      >
        <div className="flex flex-col gap-0.5 min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className={`text-[8px] ${statusColor} px-1 rounded`}>{d.status}</span>
            <span className="text-[11px] text-text">
              {d.bulletins_processed} bulletin{d.bulletins_processed !== 1 ? "s" : ""} → {d.claims_extracted} claim{d.claims_extracted !== 1 ? "s" : ""} → {d.entries_created} entr{d.entries_created !== 1 ? "ies" : "y"}
            </span>
            {d.duration_seconds != null && (
              <span className="text-[9px] text-muted/50">{d.duration_seconds.toFixed(1)}s</span>
            )}
          </div>
          {d.operations.length > 0 && !expanded && (
            <div className="flex items-center gap-1 flex-wrap">
              {d.operations.slice(0, 4).map((op, i) => (
                <span key={i} className="text-[8px] text-accent/60 bg-accent/5 px-1 rounded">
                  {op.claims}c/{op.entity_ops}e
                </span>
              ))}
              {d.operations.length > 4 && (
                <span className="text-[8px] text-muted/40">+{d.operations.length - 4} more</span>
              )}
            </div>
          )}
        </div>
        <span className="text-[9px] text-muted/50 shrink-0 mt-0.5">{relativeTime(d.created_at)}</span>
      </button>

      {expanded && (
        <div className="px-3 pb-2">
          {/* Bulletins consumed */}
          {d.bulletin_slugs.length > 0 && (
            <div className="mb-2">
              <span className="text-[9px] text-muted/50 uppercase tracking-wide">consumed bulletins</span>
              <div className="mt-1 flex flex-col gap-1.5">
                {d.bulletin_slugs.map((slug, i) => {
                  const content = bulletinContent[slug];
                  // Strip the title line and metadata lines (lines starting with "- key: value"), keep the body
                  const body = content
                    ? content.split("\n\n").slice(1).join("\n\n").trim().slice(0, 500)
                    : null;
                  return (
                    <div key={i} className="bg-surface/50 border border-border/50 rounded px-2 py-1">
                      <div className="flex items-center gap-1.5">
                        <span className="text-[8px] text-muted/50 font-mono">{slug}</span>
                        <button
                          onClick={(e) => { e.stopPropagation(); onRedigest(slug); }}
                          className="text-[8px] text-muted/40 hover:text-accent ml-auto"
                        >
                          re-digest
                        </button>
                      </div>
                      {body ? (
                        <p className="text-[10px] text-text mt-0.5 whitespace-pre-wrap">{body}</p>
                      ) : (
                        <span className="text-[9px] text-muted/40 mt-0.5 block">digested</span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Per-bulletin breakdown */}
          {d.operations.length > 0 && (
            <div className="mb-2">
              <span className="text-[9px] text-muted/50 uppercase tracking-wide">per-bulletin breakdown</span>
              <div className="mt-1 flex flex-col gap-1">
                {d.operations.map((op, i) => (
                  <div key={i} className="bg-surface/50 border border-border/50 rounded px-2 py-1">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[8px] text-muted/50 font-mono">{op.bulletin}</span>
                      <span className="text-[8px] text-accent/60 bg-accent/5 px-1 rounded">{op.claims} claims</span>
                      <span className="text-[8px] text-success/60 bg-success/5 px-1 rounded">{op.entity_ops} entity ops</span>
                      {op.source && (
                        <span className="text-[8px] text-muted/40 truncate ml-auto">{op.source.split(":").slice(-1)[0]}</span>
                      )}
                    </div>
                    {op.content_preview && (
                      <p className="text-[10px] text-text/70 mt-0.5 truncate">{op.content_preview}</p>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Raw LLM response */}
          {d.raw_response && (
            <div>
              <details>
                <summary className="text-[9px] text-muted/40 uppercase tracking-wide cursor-pointer hover:text-muted/70">
                  raw response ({d.raw_response.length} chars)
                </summary>
                <pre className="mt-1 text-[9px] text-text/60 bg-surface/80 border border-border/30 rounded px-2 py-1 max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono leading-relaxed">
                  {d.raw_response}
                </pre>
              </details>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function MemoryPage() {
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  const [lintConfirm, setLintConfirm] = useState(false);
  const queryClient = useQueryClient();

  const { data: statsData } = useQuery<MemoryStatsResponse>({
    queryKey: ["memory-stats"],
    queryFn: () => fetchAPI<MemoryStatsResponse>("/memory/stats"),
  });

  const { data: bulletinsData } = useQuery<{ bulletins: Bulletin[] }>({
    queryKey: ["memory-bulletins"],
    queryFn: () => fetchAPI<{ bulletins: Bulletin[] }>("/memory/bulletins"),
  });

  const { data: dreamsData } = useQuery<{ dreams: DreamLog[] }>({
    queryKey: ["memory-dreams"],
    queryFn: () => fetchAPI<{ dreams: DreamLog[] }>("/memory/dreams"),
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

  const redigestMutation = useMutation({
    mutationFn: async (slug: string) => {
      const secret = document.cookie.match(/cyborg_dashboard_secret=([^;]+)/)?.[1] ?? "";
      const base = import.meta.env.BASE_URL.replace(/\/$/, "");
      const res = await fetch(`${base}/api/memory/redigest?secret=${encodeURIComponent(secret)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug }),
      });
      if (!res.ok) throw new Error(`API ${res.status}`);
      return res.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memory-bulletins"] });
      queryClient.invalidateQueries({ queryKey: ["memory-stats"] });
    },
  });

  const lintMutation = useMutation({
    mutationFn: async () => {
      const secret = document.cookie.match(/cyborg_dashboard_secret=([^;]+)/)?.[1] ?? "";
      const base = import.meta.env.BASE_URL.replace(/\/$/, "");
      const res = await fetch(`${base}/api/memory/lint?secret=${encodeURIComponent(secret)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) throw new Error(`API ${res.status}`);
      return res.json();
    },
    onSuccess: () => {
      setLintConfirm(false);
      queryClient.invalidateQueries({ queryKey: ["memory-stats"] });
    },
  });

  // Auto-clear lint success after 3s
  useEffect(() => {
    if (lintMutation.isSuccess) {
      const t = setTimeout(() => lintMutation.reset(), 3000);
      return () => clearTimeout(t);
    }
  }, [lintMutation.isSuccess]);

  const stats = statsData?.stats ?? { total_entries: 0, wikis: {} };
  const bulletins = bulletinsData?.bulletins ?? [];
  const dreams = dreamsData?.dreams ?? [];
  const pendingCount = statsData?.pending_bulletins ?? 0;
  const lastDream = statsData?.last_dream ?? null;

  // Category counts for the stats line
  const categories: { name: string; count: number }[] = [];
  for (const wiki of Object.values(stats.wikis)) {
    for (const [cat, count] of Object.entries(wiki.categories)) {
      categories.push({ name: cat, count });
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header: stats + pipeline status */}
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0">
        <span className="text-xs text-text font-medium">{stats.total_entries}</span>
        <span className="text-[10px] text-muted">entries</span>
        <div className="flex items-center gap-1">
          {categories.map((c) => (
            <span key={c.name} className="text-[9px] text-accent bg-accent/10 px-1.5 py-0.5 rounded">
              {c.name} <span className="text-muted">{c.count}</span>
            </span>
          ))}
        </div>
        <button
          onClick={() => setSearchOpen(!searchOpen)}
          className="ml-auto text-[10px] text-muted hover:text-text"
        >
          search
        </button>
        <div className="relative">
          {lintMutation.isSuccess ? (
            <span className="text-[9px] text-success">linted</span>
          ) : lintConfirm ? (
            <div className="flex items-center gap-1">
              <span className="text-[8px] text-warning">rewrites all entries</span>
              <button
                onClick={() => { lintMutation.mutate(); }}
                disabled={lintMutation.isPending}
                className="text-[9px] text-error hover:underline"
              >
                {lintMutation.isPending ? "..." : "confirm"}
              </button>
              <button
                onClick={() => setLintConfirm(false)}
                className="text-[9px] text-muted hover:text-text"
              >
                cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setLintConfirm(true)}
              disabled={lintMutation.isPending}
              className="text-[10px] text-muted hover:text-text disabled:opacity-30"
            >
              lint
            </button>
          )}
        </div>
      </div>

      {/* Collapsible search */}
      {searchOpen && (
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
      )}

      {searchMutation.data && searchOpen && (
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
          {searchMutation.data.results.map((r, i) => {
            const pathParts = r.path.replace("memory/", "").replace(".md", "").split("/");
            const category = pathParts[1] || "";
            return (
              <button key={i} onClick={() => setOpenPath(r.path)} className="w-full flex items-start gap-2 py-1 hover:bg-surface/50 transition-colors text-left">
                <span className="text-[9px] text-accent bg-accent/10 px-1 rounded shrink-0 mt-0.5">{category}</span>
                <div className="flex flex-col min-w-0">
                  <span className="text-[11px] text-text">{r.title}</span>
                  {r.relevance && <span className="text-[10px] text-muted">{r.relevance}</span>}
                </div>
              </button>
            );
          })}
        </div>
      )}

      {searchMutation.isError && (
        <div className="px-3 py-1 text-[10px] text-error border-b border-border">Search failed</div>
      )}

      {/* Content viewer */}
      {openPath && (
        <ContentViewer path={openPath} onClose={() => setOpenPath(null)} />
      )}

      {/* Pending bulletins */}
      {bulletins.length > 0 && (
        <div className="shrink-0">
          <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border">
            <span className="text-[10px] text-warning font-medium">{pendingCount} queued</span>
            <span className="text-[9px] text-muted/50">waiting for next dream</span>
            {lastDream && (
              <span className="text-[9px] text-muted/40 ml-auto">last dream {relativeTime(lastDream)}</span>
            )}
          </div>
          {bulletins.map((b) => (
            <BulletinCard key={b.slug} b={b} onOpen={setOpenPath} />
          ))}
        </div>
      )}

      {/* Dream feed — the main content */}
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0">
        <span className="text-[10px] text-muted font-medium">dream log</span>
        {dreams.length > 0 && (
          <span className="text-[9px] text-muted/50">{dreams.length} runs</span>
        )}
        {bulletins.length === 0 && lastDream && (
          <span className="text-[9px] text-muted/40 ml-auto">last {relativeTime(lastDream)}</span>
        )}
      </div>

      <div className="flex-1 overflow-y-auto">
        {dreams.length === 0 ? (
          <div className="p-4 text-muted text-center text-xs">
            {bulletins.length > 0
              ? "bulletins queued — waiting for first dream"
              : "no dream activity yet"}
          </div>
        ) : (
          dreams.map((d) => (
            <DreamRunCard key={d.id} d={d} onOpen={setOpenPath} onRedigest={(slug) => redigestMutation.mutate(slug)} />
          ))
        )}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/memory/")({ component: MemoryPage });
