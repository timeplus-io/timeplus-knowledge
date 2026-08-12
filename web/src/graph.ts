// Typed client for the read-only, role-scoped graph query endpoints
// (src/tpk/graph_api.py) that back the Explorer view. Consumed by
// Explorer.tsx (entity search/detail/neighbors/subgraph) and Chat.tsx
// (source previews in the Sources panel).

import { apiFetch } from "./api";

// Mirrors src/tpk/tools.py's NODE_FIELDS exactly -- every /api/graph/search,
// /entity and /neighbors "center"/"other" node is a dict with these keys.
export type Entity = {
  id: string;
  repo: string;
  kind: string;
  name: string;
  qualified_name: string;
  file_path: string;
  line_start: number;
  line_end: number;
  summary: string | null;
  community: string | null;
  visibility: string;
  [key: string]: unknown;
};

export type NeighborRef = { id: string; name: string | null; kind: string | null };

export type NeighborEdge = {
  rel: string;
  confidence: string;
  direction: "in" | "out" | "indirect";
  other: NeighborRef;
};

export type SearchResponse = { results: Entity[] };
export type NeighborsResponse = { center: Entity; edges: NeighborEdge[] };
export type SourceResponse = {
  repo: string;
  file_path: string;
  line_start: number;
  line_end: number;
  lines: string[];
};

async function getJSON<T>(path: string): Promise<T> {
  const resp = await apiFetch(path);
  if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
  return resp.json();
}

export function searchEntities(
  q: string,
  opts: { kind?: string; repo?: string; limit?: number } = {},
): Promise<SearchResponse> {
  const params = new URLSearchParams({ q });
  if (opts.kind) params.set("kind", opts.kind);
  if (opts.repo) params.set("repo", opts.repo);
  if (opts.limit) params.set("limit", String(opts.limit));
  return getJSON(`/api/graph/search?${params}`);
}

export function getEntity(id: string): Promise<Entity> {
  return getJSON(`/api/graph/entity?${new URLSearchParams({ id })}`);
}

export function getNeighbors(
  id: string,
  opts: { direction?: "in" | "out" | "both"; depth?: number; limit?: number } = {},
): Promise<NeighborsResponse> {
  const params = new URLSearchParams({ id });
  if (opts.direction) params.set("direction", opts.direction);
  if (opts.depth) params.set("depth", String(opts.depth));
  if (opts.limit) params.set("limit", String(opts.limit));
  return getJSON(`/api/graph/neighbors?${params}`);
}

// The backend (src/tpk/graph_api.py, backed by kg.read_source) returns
// `lines` as a single newline-joined string, not an array -- see
// tests/test_graph_api.py's `lines == "line2\nline3\n"` assertion. This is
// the single seam where that string is normalized into the string[] shape
// the SourceResponse type promises and Chat.tsx/Explorer.tsx consume via
// `.map`. read_source joins lines WITH trailing newlines, so element i
// lines up with line (line_start + i) once the one trailing "\n" is
// stripped and the rest is split on "\n".
function normalizeLines(raw: string): string[] {
  if (raw === "") return [];
  const trimmed = raw.endsWith("\n") ? raw.slice(0, -1) : raw;
  return trimmed.split("\n");
}

export function readSource(
  repo: string,
  filePath: string,
  lineStart: number,
  lineEnd: number,
): Promise<SourceResponse> {
  const params = new URLSearchParams({
    repo,
    file_path: filePath,
    line_start: String(lineStart),
    line_end: String(lineEnd),
  });
  return getJSON<Omit<SourceResponse, "lines"> & { lines: string }>(
    `/api/graph/source?${params}`,
  ).then((r) => ({ ...r, lines: normalizeLines(r.lines) }));
}
