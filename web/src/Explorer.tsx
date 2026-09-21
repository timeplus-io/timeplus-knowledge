import { useEffect, useRef, useState, type FormEvent } from "react";
import { apiFetch } from "./api";
import {
  getEntity,
  getNeighbors,
  readSource,
  searchEntities,
  type Entity,
  type NeighborEdge,
  type NeighborsResponse,
  type SourceResponse,
} from "./graph";

// Real kind vocabulary emitted by the ingest pipeline (src/tpk/
// graphify_runner.py: CODE_KINDS + DOC_KINDS). Code kinds are derived from the
// graph structure (#17): "member" = a class field or declared-only method,
// "symbol" = a bare type / alias / variable reference.
const KIND_OPTIONS = ["function", "class", "member", "symbol", "file",
  "document", "paper", "image", "rationale", "concept"];

// Radial subgraph layout caps at this many neighbor nodes before folding
// the rest into a single dashed "...N more" node (mockup screen 1d).
const MAX_SUBGRAPH_NODES = 7;

// Known extraction-spec.md relation vocabulary
// (graphify/skills/*/references/extraction-spec.md), each with a natural
// forward/reverse phrasing. Anything outside this set still gets a
// reasonable label via the generic fallback in relLabel.
const REL_LABELS: Record<string, { out: string; in: string }> = {
  calls: { out: "calls →", in: "← called by" },
  implements: { out: "implements →", in: "← implemented by" },
  references: { out: "references →", in: "← referenced by" },
  cites: { out: "cites →", in: "← cited by" },
  rationale_for: { out: "rationale for →", in: "← has rationale" },
  shares_data_with: { out: "shares data with", in: "shares data with" },
  conceptually_related_to: { out: "related", in: "related" },
  semantically_similar_to: { out: "related", in: "related" },
  related: { out: "related", in: "related" },
};

function humanizeRel(rel: string): string {
  return rel.replace(/_/g, " ");
}

function relLabel(rel: string, direction: NeighborEdge["direction"]): string {
  if (direction === "indirect") return humanizeRel(rel);
  const known = REL_LABELS[rel];
  if (known) return direction === "out" ? known.out : known.in;
  const h = humanizeRel(rel);
  return direction === "out" ? `${h} →` : `← ${h}`;
}

function resultSecondary(e: Entity): string {
  if (e.file_path) {
    return e.line_start && e.kind !== "file" ? `${e.file_path}:${e.line_start}` : e.file_path;
  }
  if (e.community) return `community: ${e.community}`;
  return e.qualified_name || e.id;
}

function detailPath(e: Entity): string | null {
  if (!e.file_path) return null;
  if (!e.line_start) return e.file_path;
  const lines =
    e.line_end && e.line_end !== e.line_start
      ? `lines ${e.line_start}–${e.line_end}`
      : `line ${e.line_start}`;
  return `${e.file_path} · ${lines}`;
}

type RepoOption = { key: string; label: string };

export default function Explorer({ onAsk }: { onAsk?: (text: string) => void }) {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("");
  const [repo, setRepo] = useState("");
  const [repoOptions, setRepoOptions] = useState<RepoOption[]>([]);

  const [results, setResults] = useState<Entity[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [entity, setEntity] = useState<Entity | null>(null);
  const [neighbors, setNeighbors] = useState<NeighborsResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [source, setSource] = useState<SourceResponse | "loading" | "error" | null>(null);

  // Guards against a slow earlier search/select response clobbering a
  // faster later one (e.g. rapid filter changes or result clicks).
  const searchSeq = useRef(0);
  const selectSeq = useRef(0);

  // Best-effort repo list for the filter dropdown: GET /api/repos is
  // admin-only (src/tpk/api.py's require_admin). A non-admin gets a 403
  // here -- treated the same as "no admin-provided list" rather than an
  // error, and the dropdown still grows organically from repos seen on
  // search results / selected entities below (noteRepo).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await apiFetch("/api/repos");
        if (!resp.ok || cancelled) return;
        const data = await resp.json();
        if (!Array.isArray(data)) return;
        setRepoOptions((prev) => {
          const next = [...prev];
          for (const r of data as { enabled?: boolean; entry_key?: string }[]) {
            if (!r.enabled || !r.entry_key) continue;
            if (!next.some((o) => o.key === r.entry_key)) {
              next.push({ key: r.entry_key, label: r.entry_key });
            }
          }
          return next;
        });
      } catch {
        // Network failure -- leave repoOptions to grow from search results.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  function noteRepo(r: string | null | undefined) {
    if (!r) return;
    setRepoOptions((prev) => (prev.some((o) => o.key === r) ? prev : [...prev, { key: r, label: r }]));
  }

  async function runSearch(overrides: { kind?: string; repo?: string } = {}) {
    const q = query.trim();
    if (!q) {
      setResults([]);
      setSearchError(null);
      return;
    }
    const k = overrides.kind ?? kind;
    const rp = overrides.repo ?? repo;
    setSearching(true);
    setSearchError(null);
    const seq = ++searchSeq.current;
    try {
      const resp = await searchEntities(q, { kind: k, repo: rp, limit: 50 });
      if (seq !== searchSeq.current) return;
      setResults(resp.results);
      resp.results.forEach((r) => noteRepo(r.repo));
    } catch (err) {
      if (seq !== searchSeq.current) return;
      setSearchError(String(err));
      setResults(null);
    } finally {
      if (seq === searchSeq.current) setSearching(false);
    }
  }

  function handleSearchSubmit(e: FormEvent) {
    e.preventDefault();
    runSearch();
  }

  async function selectEntity(id: string) {
    setSelectedId(id);
    setDetailLoading(true);
    setDetailError(null);
    setSource(null);
    const seq = ++selectSeq.current;
    try {
      const [ent, nbrs] = await Promise.all([
        getEntity(id),
        getNeighbors(id, { direction: "both", depth: 1, limit: 50 }),
      ]);
      if (seq !== selectSeq.current) return;
      setEntity(ent);
      setNeighbors(nbrs);
      noteRepo(ent.repo);
    } catch (err) {
      if (seq !== selectSeq.current) return;
      setDetailError(String(err));
      setEntity(null);
      setNeighbors(null);
    } finally {
      if (seq === selectSeq.current) setDetailLoading(false);
    }
  }

  async function loadSource() {
    if (!entity || !entity.file_path) return;
    setSource("loading");
    const lineStart = entity.line_start || 1;
    const lineEnd = entity.line_end || lineStart;
    try {
      const r = await readSource(entity.repo, entity.file_path, lineStart, lineEnd);
      setSource(r);
    } catch {
      setSource("error");
    }
  }

  function askAboutEntity() {
    if (!entity || !onAsk) return;
    onAsk(`Tell me about ${entity.name}${entity.repo ? ` in ${entity.repo}` : ""}.`);
  }

  return (
    <div className="tk-explorer">
      <div className="tk-explorer-topbar">
        <div className="tk-explorer-title">Explorer</div>
        <form className="tk-explorer-search-form" onSubmit={handleSearchSubmit}>
          <input
            type="text"
            className="tk-input tk-explorer-search-input"
            placeholder="Search entities…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </form>
        <button type="button" className="tk-btn tk-explorer-search-btn" onClick={() => runSearch()}>
          Search
        </button>
        <select
          className="tk-select"
          value={kind}
          onChange={(e) => {
            const v = e.target.value;
            setKind(v);
            runSearch({ kind: v });
          }}
        >
          <option value="">kind: any</option>
          {KIND_OPTIONS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <select
          className="tk-select"
          value={repo}
          onChange={(e) => {
            const v = e.target.value;
            setRepo(v);
            runSearch({ repo: v });
          }}
        >
          <option value="">repo: any</option>
          {repoOptions.map((o) => (
            <option key={o.key} value={o.key}>
              {o.label}
            </option>
          ))}
        </select>
        <div className="tk-explorer-topbar-spacer" />
        {results !== null && (
          <div className="tk-explorer-count">
            {results.length} {results.length === 1 ? "entity" : "entities"}
          </div>
        )}
      </div>

      <div className="tk-explorer-body">
        <div className="tk-explorer-results">
          <div className="tk-explorer-results-list">
            {searching ? (
              <div className="tk-explorer-status">Searching…</div>
            ) : searchError ? (
              <div className="tk-explorer-status tk-explorer-status-error">{searchError}</div>
            ) : results === null ? (
              <div className="tk-explorer-status">Search for an entity to get started.</div>
            ) : results.length === 0 ? (
              <div className="tk-explorer-status">No results.</div>
            ) : (
              results.map((r) => (
                <button
                  type="button"
                  key={r.id}
                  className={r.id === selectedId ? "tk-result-row selected" : "tk-result-row"}
                  onClick={() => selectEntity(r.id)}
                >
                  <span className="tk-result-kind">{r.kind}</span>
                  <span className="tk-result-meta">
                    <span className="tk-result-name">{r.name}</span>
                    <span className="tk-result-path">{resultSecondary(r)}</span>
                  </span>
                </button>
              ))
            )}
          </div>
        </div>

        <div className="tk-explorer-detail">
          {detailLoading ? (
            <div className="tk-explorer-status">Loading…</div>
          ) : detailError ? (
            <div className="tk-explorer-status tk-explorer-status-error">{detailError}</div>
          ) : !entity ? (
            <div className="tk-explorer-status">Select an entity from the results to see details.</div>
          ) : (
            <>
              <div className="tk-card tk-detail-card">
                <div className="tk-detail-header">
                  <div className="tk-detail-name">{entity.name}</div>
                  <span className="tk-badge">{entity.kind}</span>
                  {entity.repo && <span className="tk-badge">{entity.repo}</span>}
                  <div className="tk-detail-spacer" />
                  <button
                    type="button"
                    className="tk-btn tk-btn-secondary"
                    disabled={!entity.file_path}
                    onClick={loadSource}
                  >
                    Read source
                  </button>
                  <button type="button" className="tk-btn" disabled={!onAsk} onClick={askAboutEntity}>
                    Ask about this
                  </button>
                </div>
                {detailPath(entity) && <div className="tk-detail-path">{detailPath(entity)}</div>}
                {entity.summary && <div className="tk-detail-summary">{entity.summary}</div>}

                {source && (
                  <div className="tk-explorer-source">
                    <div className="tk-explorer-source-header">
                      <span>Source</span>
                      <div className="tk-detail-spacer" />
                      <button type="button" className="tk-sources-close" onClick={() => setSource(null)}>
                        ✕
                      </button>
                    </div>
                    {source === "loading" ? (
                      <div className="tk-source-preview-status">Loading…</div>
                    ) : source === "error" ? (
                      <div className="tk-source-preview-status">Source unavailable</div>
                    ) : (
                      <div className="tk-source-preview">
                        {source.lines.map((line, i) => (
                          <div className="tk-source-line" key={source.line_start + i}>
                            <span className="tk-source-lineno">{source.line_start + i}</span>
                            <span>{line}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>

              <div className="tk-explorer-lower">
                <NeighborsCard neighbors={neighbors} onSelect={selectEntity} />
                <SubgraphCard entity={entity} neighbors={neighbors} onSelect={selectEntity} />
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function NeighborsCard({
  neighbors,
  onSelect,
}: {
  neighbors: NeighborsResponse | null;
  onSelect: (id: string) => void;
}) {
  const edges = neighbors?.edges ?? [];
  return (
    <div className="tk-neighbors-card">
      <div className="tk-neighbors-title">Neighbors</div>
      <div className="tk-neighbors-list">
        {edges.length === 0 ? (
          <div className="tk-explorer-status">No neighbors found.</div>
        ) : (
          edges.map((e, i) => (
            <div className="tk-neighbor-row" key={`${e.rel}-${e.direction}-${e.other.id}-${i}`}>
              <span className="tk-neighbor-rel">{relLabel(e.rel, e.direction)}</span>
              {e.other.name ? (
                <button type="button" className="tk-neighbor-link" onClick={() => onSelect(e.other.id)}>
                  {e.other.name}
                </button>
              ) : (
                <span className="tk-neighbor-link tk-neighbor-link-dangling">(out of scope)</span>
              )}
              <span className="tk-neighbor-tag">{e.confidence?.toLowerCase()}</span>
            </div>
          ))
        )}
      </div>
      {edges.length > 0 && (
        <div className="tk-neighbors-footer">
          depth 1 · {edges.length} edge{edges.length === 1 ? "" : "s"}
        </div>
      )}
    </div>
  );
}

function nodeSize(label: string): { width: number; height: number } {
  const width = Math.min(150, Math.max(56, label.length * 6.2 + 20));
  return { width, height: 24 };
}

function SubgraphCard({
  entity,
  neighbors,
  onSelect,
}: {
  entity: Entity;
  neighbors: NeighborsResponse | null;
  onSelect: (id: string) => void;
}) {
  const edges = neighbors?.edges ?? [];
  const shown = edges.slice(0, MAX_SUBGRAPH_NODES);
  const remaining = edges.length - shown.length;
  const total = shown.length + (remaining > 0 ? 1 : 0);

  const cx = 164;
  const cy = 120;
  const rx = 130;
  const ry = 78;

  const positions = Array.from({ length: total }, (_, i) => {
    const angle = -Math.PI / 2 + (i * 2 * Math.PI) / total;
    return { x: cx + rx * Math.cos(angle), y: cy + ry * Math.sin(angle) };
  });

  const centerSize = nodeSize(entity.name);

  return (
    <div className="tk-subgraph-card">
      <div className="tk-subgraph-title">Subgraph</div>
      <div className="tk-subgraph-canvas">
        <svg viewBox="0 0 328 240" preserveAspectRatio="xMidYMid meet" className="tk-subgraph-svg">
          {positions.map((p, i) => (
            <line key={`line-${i}`} x1={cx} y1={cy} x2={p.x} y2={p.y} className="tk-subgraph-line" />
          ))}

          <rect
            x={cx - centerSize.width / 2}
            y={cy - centerSize.height / 2}
            width={centerSize.width}
            height={centerSize.height}
            rx="4"
            className="tk-subgraph-node-center-rect"
          />
          <text x={cx} y={cy + 4} textAnchor="middle" className="tk-subgraph-node-center-text">
            {entity.name}
          </text>

          {shown.map((e, i) => {
            const label = e.other.name ?? e.other.kind ?? "?";
            const size = nodeSize(label);
            const { x, y } = positions[i];
            const clickable = Boolean(e.other.name);
            return (
              <g
                key={`node-${i}`}
                className={clickable ? "tk-subgraph-node tk-subgraph-node-clickable" : "tk-subgraph-node"}
                role={clickable ? "button" : undefined}
                tabIndex={clickable ? 0 : undefined}
                onClick={clickable ? () => onSelect(e.other.id) : undefined}
                onKeyDown={
                  clickable
                    ? (ev) => {
                        if (ev.key === "Enter" || ev.key === " ") {
                          ev.preventDefault();
                          onSelect(e.other.id);
                        }
                      }
                    : undefined
                }
              >
                <rect
                  x={x - size.width / 2}
                  y={y - size.height / 2}
                  width={size.width}
                  height={size.height}
                  rx="4"
                  className="tk-subgraph-node-rect"
                />
                <text x={x} y={y + 4} textAnchor="middle" className="tk-subgraph-node-text">
                  {label}
                </text>
              </g>
            );
          })}

          {remaining > 0 &&
            (() => {
              const label = `…${remaining} more`;
              const size = nodeSize(label);
              const { x, y } = positions[shown.length];
              return (
                <g className="tk-subgraph-node">
                  <rect
                    x={x - size.width / 2}
                    y={y - size.height / 2}
                    width={size.width}
                    height={size.height}
                    rx="4"
                    className="tk-subgraph-node-more-rect"
                  />
                  <text x={x} y={y + 4} textAnchor="middle" className="tk-subgraph-node-more-text">
                    {label}
                  </text>
                </g>
              );
            })()}
        </svg>
      </div>
    </div>
  );
}
