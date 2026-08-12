// Typed client for the read-only, role-scoped graph query endpoints
// (src/tpk/graph_api.py) that back the Explorer view. Stub for Task 3 —
// wired up by Task 5's Explorer.tsx.

import { apiFetch } from "./api";

export type Entity = {
  id: string;
  name: string;
  kind: string;
  repo?: string | null;
  path?: string | null;
  summary?: string | null;
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
  return getJSON(`/api/graph/source?${params}`);
}
