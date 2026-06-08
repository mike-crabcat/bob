import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useEffect } from "react";
import { fetchAPI, postAPI } from "@/lib/api";

// ── Types ──────────────────────────────────────────────────────────────────

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
  channel_id: string;
  participants: string;
  content: string;
  created_at: number;
}

interface DreamLog {
  id: string;
  bulletins_processed: number;
  entries_created: number;
  claims_extracted: number;
  bulletin_slugs: string[];
  operations: DreamOp[];
  raw_response: string;
  duration_seconds: number | null;
  status: string;
  created_at: string;
}

interface DreamOp {
  bulletin: string;
  source: string;
  claims: number | ClaimSummary[];
  entity_ops: number;
  entities_updated?: string[];
  content_preview: string;
}

interface ClaimSummary {
  id: string;
  claim_type_key: string;
  subject_id: string;
  object_id: string | null;
  value: string | null;
}

interface EntityListItem {
  entity_id: string;
  entity_type: string;
  display_name: string;
  status: string;
  updated_at: string;
  claim_count: number;
  summary: string;
}

interface EntityDetail {
  entity_id: string;
  entity_type: string;
  display_name: string;
  status: string;
  rendered: string;
  claims: ClaimDetail[];
  source_bulletins?: string[];
}

interface ClaimDetail {
  id: string;
  claim_type_key: string;
  subject_id: string;
  object_id: string | null;
  value: string | null;
  status: string;
  source_bulletins: string[];
  visibility: string;
  created_at: string | null;
}

// ── Claim colors ───────────────────────────────────────────────────────────

const CLAIM_COLORS: Record<string, string> = {
  spouse: "bg-pink-900/40 text-pink-300",
  parent: "bg-pink-900/40 text-pink-300",
  child: "bg-pink-900/40 text-pink-300",
  sibling: "bg-pink-900/40 text-pink-300",
  home_address: "bg-cyan-900/40 text-cyan-300",
  workplace: "bg-cyan-900/40 text-cyan-300",
  job: "bg-cyan-900/40 text-cyan-300",
  food_preference: "bg-orange-900/40 text-orange-300",
  drink_preference: "bg-orange-900/40 text-orange-300",
  interest: "bg-purple-900/40 text-purple-300",
  personality: "bg-purple-900/40 text-purple-300",
  language: "bg-blue-900/40 text-blue-300",
  birthday: "bg-blue-900/40 text-blue-300",
  alias: "bg-gray-900/40 text-gray-300",
  contact_id: "bg-gray-900/40 text-gray-300",
  member: "bg-green-900/40 text-green-300",
  destination: "bg-green-900/40 text-green-300",
  start_date: "bg-blue-900/40 text-blue-300",
  end_date: "bg-blue-900/40 text-blue-300",
  task_status: "bg-yellow-900/40 text-yellow-300",
  owner: "bg-yellow-900/40 text-yellow-300",
  due_date: "bg-yellow-900/40 text-yellow-300",
  description: "bg-gray-900/40 text-gray-300",
  location: "bg-cyan-900/40 text-cyan-300",
  transport_type: "bg-cyan-900/40 text-cyan-300",
  decision: "bg-green-900/40 text-green-300",
  rationale: "bg-green-900/40 text-green-300",
  purpose: "bg-indigo-900/40 text-indigo-300",
  name: "bg-blue-900/40 text-blue-300",
  stop: "bg-teal-900/40 text-teal-300",
  file_path: "bg-amber-900/40 text-amber-300",
  file_ref: "bg-amber-900/40 text-amber-300",
  thing_type: "bg-lime-900/40 text-lime-300",
  truth: "bg-rose-900/40 text-rose-300",
};

// ── Helpers ────────────────────────────────────────────────────────────────

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

// ── Tab types ──────────────────────────────────────────────────────────────

type Tab = "entities" | "pipeline" | "search" | "stats" | "qa";

// ── Question types ──────────────────────────────────────────────────────────

interface Question {
  id: string;
  entity_id: string;
  question: string;
  options: string[];
  context: string;
  status: string;
  answer: string | null;
  created_at: string | null;
  answered_at: string | null;
}

interface QuestionsResponse {
  questions: Question[];
}

// ── BulletinCard ───────────────────────────────────────────────────────────

function BulletinCard({ b }: { b: Bulletin }) {
  const [expanded, setExpanded] = useState(false);
  const firstLine = b.content.split("\n")[0].replace(/^#\s*/, "").replace(/^-\s*/, "");
  return (
    <div className="border-b border-border/50">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-start gap-2 px-3 py-2 hover:bg-surface/30 transition-colors text-left"
      >
        <span className="text-[8px] text-warning/80 bg-warning/10 px-1 rounded shrink-0 mt-0.5">queued</span>
        <div className="flex flex-col min-w-0 flex-1">
          <Link
            to="/memory/bulletins/$bulletinId"
            params={{ bulletinId: b.slug }}
            onClick={(e) => e.stopPropagation()}
            className="text-[11px] text-text hover:text-accent truncate"
          >
            {firstLine || b.slug}
          </Link>
          {b.participants && (
            <span className="text-[9px] text-muted/60">{b.participants}</span>
          )}
        </div>
        <span className="text-[9px] text-muted/50 shrink-0 mt-0.5">{relativeTimeEpoch(b.created_at)}</span>
      </button>
      {expanded && (
        <div className="px-3 pb-2">
          <div className="flex items-center gap-1.5 mb-1">
            <Link to="/memory/bulletins/$bulletinId" params={{ bulletinId: b.slug }} className="text-[8px] text-accent/60 font-mono hover:underline">{b.slug}</Link>
            <span className="text-[8px] text-accent/60 bg-accent/5 px-1 rounded">{b.source_type}</span>
          </div>
          <pre className="text-[10px] text-text whitespace-pre-wrap break-words font-mono leading-relaxed max-h-48 overflow-y-auto">{b.content}</pre>
        </div>
      )}
    </div>
  );
}

// ── DreamRunCard ───────────────────────────────────────────────────────────

function DreamRunCard({
  d,
  onRedigest,
  onNavigateEntity,
}: {
  d: DreamLog;
  onRedigest: (slug: string) => void;
  onNavigateEntity: (entityId: string) => void;
}) {
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

  // Determine if ops have enriched data (new format) vs counts (old format)
  const hasEnrichedOps = d.operations.length > 0 && Array.isArray(d.operations[0]?.claims);

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
                  {typeof op.claims === "number" ? op.claims : op.claims.length}c/{op.entity_ops}e
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
          {/* Per-bulletin breakdown with full content and claims */}
          {d.operations.length > 0 && (
            <div className="mb-2">
              <span className="text-[9px] text-muted/50 uppercase tracking-wide">per-bulletin breakdown</span>
              <div className="mt-1 flex flex-col gap-2">
                {d.operations.map((op, i) => {
                  const content = bulletinContent[op.bulletin];
                  const claimList = Array.isArray(op.claims) ? op.claims : [];
                  const claimCount = typeof op.claims === "number" ? op.claims : claimList.length;
                  return (
                    <div key={i} className="bg-surface/50 border border-border/50 rounded px-2 py-1.5">
                      <div className="flex items-center gap-1.5 mb-1">
                        <Link to="/memory/bulletins/$bulletinId" params={{ bulletinId: op.bulletin }} className="text-[8px] text-accent/60 font-mono hover:underline">{op.bulletin}</Link>
                        <span className="text-[8px] text-accent/60 bg-accent/5 px-1 rounded">{claimCount} claims</span>
                        <span className="text-[8px] text-success/60 bg-success/5 px-1 rounded">{op.entity_ops} entity ops</span>
                        {op.source && (
                          <span className="text-[8px] text-muted/40 truncate ml-auto">{op.source.split(":").slice(-1)[0]}</span>
                        )}
                        <button
                          onClick={(e) => { e.stopPropagation(); onRedigest(op.bulletin); }}
                          className="text-[8px] text-muted/40 hover:text-accent ml-1"
                        >
                          re-digest
                        </button>
                      </div>

                      {/* Full bulletin content */}
                      {content && (
                        <pre className="text-[10px] text-text/80 whitespace-pre-wrap break-words font-mono leading-relaxed max-h-32 overflow-y-auto mb-1.5 bg-surface/80 border border-border/30 rounded px-1.5 py-1">
                          {content}
                        </pre>
                      )}

                      {/* Extracted claims (new format) */}
                      {claimList.length > 0 && (
                        <div className="flex flex-col gap-0.5 mb-1">
                          <span className="text-[8px] text-muted/40 uppercase tracking-wide">extracted claims</span>
                          {claimList.map((c, ci) => (
                            <div key={ci} className="flex items-center gap-1.5 pl-1">
                              <span className={`text-[8px] px-1 rounded ${CLAIM_COLORS[c.claim_type_key] ?? "bg-gray-900/40 text-gray-300"}`}>
                                {c.claim_type_key}
                              </span>
                              <span className="text-[10px] text-text truncate">
                                {c.subject_id}
                                {c.object_id ? ` → ${c.object_id}` : c.value ? ` → ${c.value}` : ""}
                              </span>
                            </div>
                          ))}
                        </div>
                      )}

                      {/* Entities updated (new format) */}
                      {op.entities_updated && op.entities_updated.length > 0 && (
                        <div className="flex items-center gap-1 flex-wrap">
                          <span className="text-[8px] text-muted/40">entities:</span>
                          {op.entities_updated.map((eid, ei) => (
                            <button
                              key={ei}
                              onClick={() => onNavigateEntity(eid)}
                              className="text-[8px] text-accent hover:underline bg-accent/5 px-1 rounded"
                            >
                              {eid}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
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

// ── Entity Detail View ─────────────────────────────────────────────────────

function EntityDetailView({
  entity,
  onBack,
  onNavigateEntity,
}: {
  entity: EntityDetail;
  onBack: () => void;
  onNavigateEntity: (entityId: string) => void;
}) {
  const [expandedClaim, setExpandedClaim] = useState<string | null>(null);
  const [bulletinContent, setBulletinContent] = useState<Record<string, string>>({});
  const [fetchedBulletins, setFetchedBulletins] = useState(false);
  const [merging, setMerging] = useState(false);
  const [mergeTarget, setMergeTarget] = useState("");
  const [mergeBusy, setMergeBusy] = useState(false);

  const fetchBulletins = async () => {
    if (fetchedBulletins || !(entity.source_bulletins?.length)) return;
    try {
      const secret = document.cookie.match(/cyborg_dashboard_secret=([^;]+)/)?.[1] ?? "";
      const base = import.meta.env.BASE_URL.replace(/\/$/, "");
      const res = await fetch(`${base}/api/memory/digested?secret=${encodeURIComponent(secret)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slugs: entity.source_bulletins ?? [] }),
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

  useEffect(() => {
    fetchBulletins();
  }, [entity.entity_id]);

  const hasRelated = false;

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0">
        <button onClick={onBack} className="text-[10px] text-accent hover:underline">&larr; all entities</button>
        <span className="text-xs text-text font-medium truncate flex-1">{entity.display_name}</span>
        <span className="text-[8px] text-accent/60 bg-accent/10 px-1.5 py-0.5 rounded">{entity.entity_type}</span>
        <span className="text-[8px] text-success/60 bg-success/10 px-1.5 py-0.5 rounded">{entity.status}</span>
        {!merging && (
          <button
            onClick={() => setMerging(true)}
            className="text-[8px] text-muted hover:text-accent bg-surface/50 border border-border/50 px-1.5 py-0.5 rounded hover:border-accent/30 transition-colors"
          >merge</button>
        )}
      </div>

      {/* Merge bar */}
      {merging && (
        <div className="flex items-center gap-1.5 px-3 py-1 border-b border-border bg-warning/5 shrink-0">
          <span className="text-[9px] text-muted">Merge into:</span>
          <input
            value={mergeTarget}
            onChange={(e) => setMergeTarget(e.target.value)}
            placeholder="canonical entity ID"
            className="flex-1 text-[10px] bg-surface border border-border/50 rounded px-1.5 py-0.5 text-text font-mono placeholder:text-muted/30 focus:outline-none focus:border-accent/40"
          />
          <button
            disabled={mergeBusy || !mergeTarget.trim()}
            onClick={async () => {
              setMergeBusy(true);
              try {
                const secret = document.cookie.match(/cyborg_dashboard_secret=([^;]+)/)?.[1] ?? "";
                const base = import.meta.env.BASE_URL.replace(/\/$/, "");
                const res = await fetch(`${base}/api/memory/entities/merge?secret=${encodeURIComponent(secret)}`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ canonical_id: mergeTarget.trim(), loser_id: entity.entity_id }),
                });
                const data = await res.json();
                if (!res.ok || data.error) throw new Error(data.error || "merge failed");
                onNavigateEntity(mergeTarget.trim());
              } catch {
                alert("Merge failed");
              } finally {
                setMergeBusy(false);
              }
            }}
            className="text-[8px] text-warning bg-warning/10 border border-warning/30 px-1.5 py-0.5 rounded hover:bg-warning/20 disabled:opacity-40 transition-colors"
          >{mergeBusy ? "..." : "confirm"}</button>
          <button
            onClick={() => { setMerging(false); setMergeTarget(""); }}
            className="text-[8px] text-muted hover:text-text px-1"
          >cancel</button>
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {/* Rendered body */}
        {entity.rendered && (
          <div className="px-3 py-2 border-b border-border">
            <pre className="text-[11px] text-text whitespace-pre-wrap break-words font-mono leading-relaxed">
              {entity.rendered}
            </pre>
          </div>
        )}

        {/* Claims */}
        {entity.claims.length > 0 && (
          <div className="px-3 py-2 border-b border-border">
            <span className="text-[9px] text-muted/50 uppercase tracking-wide">claims ({entity.claims.length})</span>
            <div className="mt-1 flex flex-col gap-1">
              {entity.claims.map((c) => {
                const isSubject = c.subject_id === entity.entity_id;
                const otherEntity = isSubject ? c.object_id : c.subject_id;
                const dir = isSubject ? "→" : "←";
                return (
                  <div
                    key={c.id}
                    onClick={() => setExpandedClaim(expandedClaim === c.id ? null : c.id)}
                    className="bg-surface/50 border border-border/50 px-2 py-1 cursor-pointer hover:border-accent/30 transition-colors rounded"
                  >
                    <div className="flex items-center gap-1.5">
                      <span className={`text-[9px] px-1 rounded ${CLAIM_COLORS[c.claim_type_key] ?? "bg-gray-900/40 text-gray-300"}`}>
                        {c.claim_type_key}
                      </span>
                      <span className="text-[11px] text-text flex-1 overflow-hidden">
                        {otherEntity ? (
                          <>
                            <span className="text-muted/60 mr-1">{dir}</span>
                            <button
                              onClick={(e) => { e.stopPropagation(); onNavigateEntity(otherEntity); }}
                              className="text-accent hover:underline"
                            >
                              {otherEntity.length > 40 ? otherEntity.slice(0, -6) + ".." + otherEntity.slice(-4) : otherEntity}
                            </button>
                          </>
                        ) : c.value ? (
                          <>{dir} {c.value}</>
                        ) : ""}
                      </span>
                      {c.created_at && (
                        <span className="text-[9px] text-muted/40 shrink-0">{new Date(c.created_at).toLocaleDateString()}</span>
                      )}
                    </div>
                    {expandedClaim === c.id && (
                      <div className="mt-1 text-[10px] text-muted border-t border-border/30 pt-1 flex flex-col gap-0.5">
                        <div>
                          <span className="text-muted/50">from:</span>{" "}
                          <button onClick={(e) => { e.stopPropagation(); onNavigateEntity(c.subject_id); }} className="text-accent hover:underline">{c.subject_id}</button>
                        </div>
                        {c.object_id && (
                          <div>
                            <span className="text-muted/50">to:</span>{" "}
                            <button onClick={(e) => { e.stopPropagation(); onNavigateEntity(c.object_id!); }} className="text-accent hover:underline">{c.object_id}</button>
                          </div>
                        )}
                        {c.value && (
                          <div>
                            <span className="text-muted/50">value:</span> {c.value}
                          </div>
                        )}
                        <div>
                          <span className="text-muted/50">vis:</span> {c.visibility}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Related entities */}
        {hasRelated && (
          <div className="px-3 py-2 border-b border-border">
            <span className="text-[9px] text-muted/50 uppercase tracking-wide">related entities</span>
            <div className="mt-1 flex flex-col gap-1">
              {Object.entries(entity.related_entities).map(([cat, ids]) =>
                ids && ids.length > 0 ? (
                  <div key={cat}>
                    <span className="text-[8px] text-muted/40 uppercase">{cat}</span>
                    <div className="flex flex-wrap gap-1 mt-0.5">
                      {ids.map((eid) => (
                        <button
                          key={eid}
                          onClick={() => onNavigateEntity(eid)}
                          className="text-[9px] text-accent hover:underline bg-accent/5 px-1.5 py-0.5 rounded"
                        >
                          {eid}
                        </button>
                      ))}
                    </div>
                  </div>
                ) : null
              )}
            </div>
          </div>
        )}

        {/* Source bulletins */}
        {(entity.source_bulletins?.length ?? 0) > 0 && (
          <div className="px-3 py-2">
            <span className="text-[9px] text-muted/50 uppercase tracking-wide">source bulletins ({entity.source_bulletins?.length ?? 0})</span>
            <div className="mt-1 flex flex-col gap-1">
              {(entity.source_bulletins ?? []).map((slug) => (
                <details key={slug}>
                  <summary className="text-[9px] font-mono cursor-pointer hover:text-muted/70">
                    <Link
                      to="/memory/bulletins/$bulletinId"
                      params={{ bulletinId: slug }}
                      className="text-accent/60 hover:underline"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {slug}
                    </Link>
                  </summary>
                  <pre className="mt-1 text-[10px] text-text/80 whitespace-pre-wrap break-words font-mono leading-relaxed max-h-32 overflow-y-auto bg-surface/50 border border-border/30 rounded px-1.5 py-1">
                    {bulletinContent[slug] || "loading..."}
                  </pre>
                </details>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Question Cards ────────────────────────────────────────────────────────

function QuestionCard({
  q,
  onAnswer,
  onDismiss,
  onNavigateEntity,
  isSubmitting,
}: {
  q: Question;
  onAnswer: (answer: string) => void;
  onDismiss: () => void;
  onNavigateEntity: (entityId: string) => void;
  isSubmitting: boolean;
}) {
  const [customAnswer, setCustomAnswer] = useState("");

  return (
    <div className="px-3 py-2 border-b border-border/50">
      <div className="flex items-start gap-1.5 mb-1">
        <span className="text-[8px] text-warning bg-warning/10 px-1 rounded shrink-0 mt-0.5">open</span>
        <span className="text-[11px] text-text flex-1">{q.question}</span>
        <button
          onClick={onDismiss}
          disabled={isSubmitting}
          className="text-[8px] text-muted/50 hover:text-danger px-1 shrink-0 mt-0.5 disabled:opacity-30"
          title="Dismiss"
        >
          dismiss
        </button>
      </div>
      {q.context && (
        <div className="text-[9px] text-muted/60 mb-1.5 pl-4">{q.context}</div>
      )}
      <div className="flex items-center gap-1 mb-2 pl-4">
        <span className="text-[8px] text-muted/40">entity:</span>
        <button
          onClick={() => onNavigateEntity(q.entity_id)}
          className="text-[9px] text-accent hover:underline"
        >
          {q.entity_id}
        </button>
      </div>
      {q.options.length > 0 && (
        <div className="flex flex-wrap gap-1 mb-2 pl-4">
          {q.options.map((opt) => (
            <button
              key={opt}
              onClick={() => onAnswer(opt)}
              disabled={isSubmitting}
              className="text-[10px] px-2 py-0.5 border border-border text-muted hover:text-text hover:border-accent transition-colors rounded disabled:opacity-50"
            >
              {isSubmitting ? "..." : opt}
            </button>
          ))}
        </div>
      )}
      <div className="flex gap-1 pl-4">
        <input
          type="text"
          value={customAnswer}
          onChange={(e) => setCustomAnswer(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && customAnswer.trim() && !isSubmitting) {
              onAnswer(customAnswer.trim());
              setCustomAnswer("");
            }
          }}
          placeholder="Custom answer..."
          disabled={isSubmitting}
          className="flex-1 text-[10px] bg-transparent border border-border px-2 py-0.5 text-text placeholder:text-muted/50 focus:outline-none focus:border-accent disabled:opacity-50"
        />
        <button
          onClick={() => {
            if (customAnswer.trim() && !isSubmitting) {
              onAnswer(customAnswer.trim());
              setCustomAnswer("");
            }
          }}
          disabled={!customAnswer.trim() || isSubmitting}
          className="px-2 py-0.5 text-[10px] border border-border text-muted hover:text-text hover:border-accent transition-colors disabled:opacity-30"
        >
          {isSubmitting ? "..." : "send"}
        </button>
      </div>
    </div>
  );
}

function AnsweredQuestionCard({
  q,
  onNavigateEntity,
}: {
  q: Question;
  onNavigateEntity: (entityId: string) => void;
}) {
  return (
    <div className="px-3 py-2 border-b border-border/50">
      <div className="flex items-start gap-1.5 mb-1">
        <span className="text-[8px] text-success bg-success/10 px-1 rounded shrink-0 mt-0.5">answered</span>
        <span className="text-[11px] text-text/70 flex-1">{q.question}</span>
      </div>
      <div className="pl-4 flex flex-col gap-0.5">
        <div className="text-[10px] text-text">{q.answer}</div>
        <div className="flex items-center gap-1">
          <span className="text-[8px] text-muted/40">entity:</span>
          <button
            onClick={() => onNavigateEntity(q.entity_id)}
            className="text-[9px] text-accent/60 hover:underline"
          >
            {q.entity_id}
          </button>
          {q.answered_at && (
            <span className="text-[8px] text-muted/40 ml-auto">{relativeTime(q.answered_at)}</span>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Main Memory Page ───────────────────────────────────────────────────────

function MemoryPage() {
  const [tab, setTab] = useState<Tab>("entities");
  const [selectedType, setSelectedType] = useState<string | null>(null);
  const [selectedEntity, setSelectedEntity] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");

  const queryClient = useQueryClient();

  // ── Data fetching ──

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

  const { data: entitiesData } = useQuery<{ entities: EntityListItem[] }>({
    queryKey: ["memory-entities", selectedType],
    queryFn: () => {
      const path = selectedType ? `/memory/entities?type=${encodeURIComponent(selectedType)}` : "/memory/entities";
      return fetchAPI<{ entities: EntityListItem[] }>(path);
    },
    enabled: tab === "entities",
  });

  const { data: entityDetail } = useQuery<EntityDetail | { error: string }>({
    queryKey: ["memory-entity", selectedEntity],
    queryFn: () => fetchAPI<EntityDetail>(`/memory/entities/${encodeURIComponent(selectedEntity!)}`),
    enabled: tab === "entities" && selectedEntity !== null,
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

  // ── Questions data ──

  const { data: openQuestionsData } = useQuery<QuestionsResponse>({
    queryKey: ["memory-questions-open"],
    queryFn: () => fetchAPI<QuestionsResponse>("/memory/questions?status=open"),
    enabled: tab === "qa",
  });

  const { data: answeredQuestionsData } = useQuery<QuestionsResponse>({
    queryKey: ["memory-questions-answered"],
    queryFn: () => fetchAPI<QuestionsResponse>("/memory/questions?status=answered"),
    enabled: tab === "qa",
  });

  const answerMutation = useMutation({
    mutationFn: async ({ id, answer }: { id: string; answer: string }) => {
      return postAPI(`/memory/questions/${id}/answer`, { answer });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memory-questions-open"] });
      queryClient.invalidateQueries({ queryKey: ["memory-questions-answered"] });
    },
  });

  const dismissMutation = useMutation({
    mutationFn: async ({ id }: { id: string }) => {
      return postAPI(`/memory/questions/${id}/dismiss`, {});
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memory-questions-open"] });
    },
  });

  // ── Derived data ──

  const stats = statsData?.stats ?? { total_entries: 0, wikis: {} };
  const bulletins = bulletinsData?.bulletins ?? [];
  const dreams = dreamsData?.dreams ?? [];
  const entities = entitiesData?.entities ?? [];
  const pendingCount = statsData?.pending_bulletins ?? 0;
  const lastDream = statsData?.last_dream ?? null;

  const categories: { name: string; count: number }[] = [];
  for (const wiki of Object.values(stats.wikis)) {
    for (const [cat, count] of Object.entries(wiki.categories)) {
      categories.push({ name: cat, count });
    }
  }

  const handleSearch = () => {
    const q = searchInput.trim();
    if (q) searchMutation.mutate(q);
  };

  const navigateToEntity = (entityId: string) => {
    setSelectedEntity(entityId);
    setTab("entities");
  };

  const isEntityDetail = entityDetail && !("error" in entityDetail);

  // ── Render ──

  return (
    <div className="flex flex-col h-full">
      {/* Tab bar */}
      <div className="flex items-center gap-0 px-3 border-b border-border shrink-0">
        {(["entities", "pipeline", "search", "stats", "qa"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-3 py-1.5 text-[10px] border-b-2 transition-colors ${
              tab === t
                ? "border-accent text-text font-medium"
                : "border-transparent text-muted hover:text-text"
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-hidden">
        {/* ── Entities Tab ── */}
        {tab === "entities" && (
          selectedEntity && isEntityDetail ? (
            <EntityDetailView
              entity={entityDetail as EntityDetail}
              onBack={() => setSelectedEntity(null)}
              onNavigateEntity={navigateToEntity}
            />
          ) : (
            <div className="flex flex-col h-full">
              {/* Type filter bar */}
              <div className="flex items-center gap-1 px-3 py-1.5 border-b border-border shrink-0 overflow-x-auto">
                <button
                  onClick={() => setSelectedType(null)}
                  className={`text-[9px] px-1.5 py-0.5 rounded shrink-0 ${
                    selectedType === null
                      ? "bg-accent/20 text-accent"
                      : "bg-surface/50 text-muted hover:text-text"
                  }`}
                >
                  all ({stats.total_entries})
                </button>
                {categories.map((c) => (
                  <button
                    key={c.name}
                    onClick={() => setSelectedType(c.name)}
                    className={`text-[9px] px-1.5 py-0.5 rounded shrink-0 ${
                      selectedType === c.name
                        ? "bg-accent/20 text-accent"
                        : "bg-surface/50 text-muted hover:text-text"
                    }`}
                  >
                    {c.name} ({c.count})
                  </button>
                ))}
              </div>

              {/* Entity list */}
              <div className="flex-1 overflow-y-auto">
                {entities.length === 0 ? (
                  <div className="p-4 text-muted text-center text-xs">no entities</div>
                ) : (
                  entities.map((e) => (
                    <button
                      key={e.entity_id}
                      onClick={() => setSelectedEntity(e.entity_id)}
                      className="w-full flex items-center gap-2 px-3 py-1.5 border-b border-border/50 hover:bg-surface/30 transition-colors text-left"
                    >
                      <span className="text-[8px] text-accent/60 bg-accent/10 px-1 rounded shrink-0">{e.entity_type}</span>
                      <div className="flex flex-col min-w-0 flex-1">
                        <span className="text-[11px] text-text truncate">{e.display_name || e.entity_id}</span>
                        <span className="text-[9px] text-muted/40 font-mono truncate">{e.summary || e.entity_id}</span>
                      </div>
                      {e.claim_count > 0 && (
                        <span className="text-[8px] text-muted/50 shrink-0">{e.claim_count} claims</span>
                      )}
                      <span className="text-[9px] text-muted/40 shrink-0">{relativeTime(e.updated_at)}</span>
                    </button>
                  ))
                )}
              </div>
            </div>
          )
        )}

        {/* ── Pipeline Tab ── */}
        {tab === "pipeline" && (
          <div className="flex flex-col h-full overflow-y-auto">
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
                  <BulletinCard key={b.slug} b={b} />
                ))}
              </div>
            )}

            {/* Dream feed */}
            <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0">
              <span className="text-[10px] text-muted font-medium">dream log</span>
              {dreams.length > 0 && (
                <span className="text-[9px] text-muted/50">{dreams.length} runs</span>
              )}
              {bulletins.length === 0 && lastDream && (
                <span className="text-[9px] text-muted/40 ml-auto">last {relativeTime(lastDream)}</span>
              )}
            </div>

            {dreams.length === 0 ? (
              <div className="p-4 text-muted text-center text-xs">
                {bulletins.length > 0
                  ? "bulletins queued — waiting for first dream"
                  : "no dream activity yet"}
              </div>
            ) : (
              dreams.map((d) => (
                <DreamRunCard
                  key={d.id}
                  d={d}
                  onRedigest={(slug) => redigestMutation.mutate(slug)}
                  onNavigateEntity={navigateToEntity}
                />
              ))
            )}
          </div>
        )}

        {/* ── Search Tab ── */}
        {tab === "search" && (
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

            {searchMutation.isError && (
              <div className="px-3 py-1 text-[10px] text-error border-b border-border">Search failed</div>
            )}

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
                {searchMutation.data.results.map((r, i) => {
                  const pathParts = r.path.replace("memory/", "").replace(".md", "").split("/");
                  const category = pathParts[1] || "";
                  return (
                    <button
                      key={i}
                      onClick={() => {
                        // Try to navigate to entity if it looks like an entity_id
                        const slug = r.path.split("/").pop()?.replace(".md", "") || "";
                        if (slug.startsWith("person-") || slug.startsWith("contact-") || slug.startsWith("group-") || slug.startsWith("trip-") || slug.startsWith("file-") || slug.startsWith("thing-")) {
                          navigateToEntity(slug);
                        }
                      }}
                      className="w-full flex items-start gap-2 py-1 hover:bg-surface/50 transition-colors text-left"
                    >
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

            {!searchMutation.data && !searchMutation.isPending && (
              <div className="p-4 text-muted text-center text-xs">search memory entities by meaning</div>
            )}
          </div>
        )}

        {/* ── Stats Tab ── */}
        {tab === "stats" && (
          <div className="flex flex-col h-full overflow-y-auto">
            <div className="px-3 py-3">
              <div className="text-xs text-text font-medium mb-2">
                {stats.total_entries} entities
              </div>
              <div className="flex flex-col gap-1">
                {categories
                  .sort((a, b) => b.count - a.count)
                  .map((c) => {
                    const pct = stats.total_entries > 0 ? (c.count / stats.total_entries) * 100 : 0;
                    return (
                      <button
                        key={c.name}
                        onClick={() => { setSelectedType(c.name); setTab("entities"); }}
                        className="flex items-center gap-2 w-full text-left hover:bg-surface/50 transition-colors py-0.5"
                      >
                        <span className="text-[10px] text-muted w-20 shrink-0">{c.name}</span>
                        <div className="flex-1 h-3 bg-surface border border-border">
                          <div className="h-full bg-accent/40" style={{ width: `${pct}%` }} />
                        </div>
                        <span className="text-[10px] text-text w-6 text-right">{c.count}</span>
                      </button>
                    );
                  })}
              </div>

              <div className="mt-4 pt-3 border-t border-border">
                <div className="text-[10px] text-muted uppercase mb-1">Pipeline</div>
                <div className="flex items-center gap-3 text-[10px]">
                  <span className="text-muted">pending bulletins: <span className="text-text">{pendingCount}</span></span>
                  {lastDream && <span className="text-muted">last dream: <span className="text-text">{lastDream}</span></span>}
                </div>
              </div>

              <div className="mt-4 pt-3 border-t border-border">
                <div className="text-[10px] text-muted uppercase mb-1">Claims</div>
                <div className="text-[10px] text-muted">
                  {(() => {
                    const totalClaims = categories.reduce((s, c) => s + c.count, 0);
                    return <span className="text-text">{totalClaims}</span>;
                  })()}{" "}
                  entity records
                </div>
              </div>
            </div>
          </div>
        )}

        {/* ── QA Tab ── */}
        {tab === "qa" && (
          <div className="flex flex-col h-full overflow-y-auto">
            {/* Outstanding */}
            <div className="border-b border-border">
              <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border/50">
                <span className="text-[10px] text-warning font-medium">outstanding</span>
                <span className="text-[9px] text-muted/50">{openQuestionsData?.questions.length ?? 0} question{(openQuestionsData?.questions.length ?? 0) !== 1 ? "s" : ""}</span>
              </div>
              {(openQuestionsData?.questions.length ?? 0) === 0 ? (
                <div className="px-3 py-4 text-muted text-center text-xs">no open questions</div>
              ) : (
                openQuestionsData!.questions.map((q) => (
                  <QuestionCard key={q.id} q={q} onAnswer={(answer) => answerMutation.mutate({ id: q.id, answer })} onDismiss={() => dismissMutation.mutate({ id: q.id })} onNavigateEntity={navigateToEntity} isSubmitting={answerMutation.isPending || dismissMutation.isPending} />
                ))
              )}
            </div>

            {/* Answered */}
            <div>
              <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border/50">
                <span className="text-[10px] text-muted font-medium">answered</span>
                <span className="text-[9px] text-muted/50">{answeredQuestionsData?.questions.length ?? 0}</span>
              </div>
              {(answeredQuestionsData?.questions.length ?? 0) === 0 ? (
                <div className="px-3 py-4 text-muted text-center text-xs">no answered questions</div>
              ) : (
                answeredQuestionsData!.questions.map((q) => (
                  <AnsweredQuestionCard key={q.id} q={q} onNavigateEntity={navigateToEntity} />
                ))
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/memory/")({ component: MemoryPage });
