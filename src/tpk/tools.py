"""KnowledgeGraph: the single read path over the kg_* streams.

Consumed by the MCP server (this plan) and the LangGraph agent (follow-up plan).
"""

from collections import deque
from pathlib import Path

NODE_FIELDS = [
    "id", "repo", "kind", "name", "qualified_name", "file_path",
    "line_start", "line_end", "summary", "community", "visibility",
]
EDGE_FIELDS = ["src", "dst", "rel", "confidence", "repo"]


class KnowledgeGraph:
    MAX_DEPTH = 3
    MAX_NODES_PER_HOP = 200

    def __init__(self, client, stream_prefix: str = "", repo_paths: dict[str, Path] | None = None):
        self.client = client
        self.prefix = stream_prefix
        self.repo_paths = repo_paths or {}

    # -- internals ---------------------------------------------------------

    def _nodes_by_ids(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        rows = self.client.query(
            f"SELECT {', '.join(NODE_FIELDS)} FROM table({self.prefix}kg_nodes)"
            " WHERE id IN %(ids)s",
            parameters={"ids": ids},
        ).result_rows
        return [dict(zip(NODE_FIELDS, r)) for r in rows]

    def _edges_touching(self, ids: list[str], rels, direction: str, confidence) -> list[dict]:
        clauses, params = [], {"ids": ids}
        if direction == "out":
            clauses.append("src IN %(ids)s")
        elif direction == "in":
            clauses.append("dst IN %(ids)s")
        else:
            clauses.append("(src IN %(ids)s OR dst IN %(ids)s)")
        if rels:
            clauses.append("rel IN %(rels)s")
            params["rels"] = rels
        if confidence:
            clauses.append("confidence = %(conf)s")
            params["conf"] = confidence
        rows = self.client.query(
            f"SELECT {', '.join(EDGE_FIELDS)} FROM table({self.prefix}kg_edges)"
            f" WHERE {' AND '.join(clauses)}",
            parameters=params,
        ).result_rows
        return [dict(zip(EDGE_FIELDS, r)) for r in rows]

    # -- public tools ------------------------------------------------------

    def search_entities(self, query, kinds=None, repos=None, limit=20):
        tokens = [t for t in query.split() if t]
        if not tokens:
            return []
        clauses, params = [], {"q": query.lower(), "limit": limit}
        for i, tok in enumerate(tokens):
            params[f"t{i}"] = f"%{tok.lower()}%"
            clauses.append(
                f"(lower(name) LIKE %(t{i})s OR lower(qualified_name) LIKE %(t{i})s"
                f" OR lower(summary) LIKE %(t{i})s)"
            )
        if kinds:
            clauses.append("kind IN %(kinds)s")
            params["kinds"] = kinds
        if repos:
            clauses.append("repo IN %(repos)s")
            params["repos"] = repos
        rows = self.client.query(
            f"SELECT {', '.join(NODE_FIELDS)} FROM table({self.prefix}kg_nodes)"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY (lower(name) = %(q)s) DESC, length(name) ASC"
            " LIMIT %(limit)s",
            parameters=params,
        ).result_rows
        return [dict(zip(NODE_FIELDS, r)) for r in rows]

    def get_entity(self, entity_id: str):
        hits = self._nodes_by_ids([entity_id])
        return hits[0] if hits else None

    def neighbors(self, entity_id, rels=None, direction="both", depth=1, confidence=None):
        depth_used = min(max(depth, 1), self.MAX_DEPTH)
        seen = {entity_id}
        frontier = [entity_id]
        all_edges: dict[tuple, dict] = {}
        for _ in range(depth_used):
            if not frontier:
                break
            edges = self._edges_touching(frontier, rels, direction, confidence)
            next_frontier = set()
            for e in edges:
                all_edges[(e["src"], e["dst"], e["rel"])] = e
                for endpoint in (e["src"], e["dst"]):
                    if endpoint not in seen:
                        next_frontier.add(endpoint)
            frontier = sorted(next_frontier)[: self.MAX_NODES_PER_HOP]
            seen.update(frontier)
        # Truncating the frontier to MAX_NODES_PER_HOP can leave `all_edges`
        # referencing endpoints that never made it into `seen` (and thus
        # never into the returned nodes) -- drop those so every returned
        # edge's src/dst is always among the returned nodes.
        edges = [e for e in all_edges.values() if e["src"] in seen and e["dst"] in seen]
        return {
            "nodes": self._nodes_by_ids(sorted(seen)),
            "edges": edges,
            "depth_used": depth_used,
        }

    def path_between(self, id_a, id_b, max_depth: int = 4):
        if id_a == id_b:
            return self._nodes_by_ids([id_a])
        parents: dict[str, tuple[str, dict]] = {}
        seen = {id_a}
        queue = deque([(id_a, 0)])
        while queue:
            current, dist = queue.popleft()
            if dist >= max_depth:
                continue
            for e in self._edges_touching([current], None, "both", None):
                for nxt in (e["src"], e["dst"]):
                    if nxt in seen:
                        continue
                    seen.add(nxt)
                    parents[nxt] = (current, e)
                    if nxt == id_b:
                        return self._materialize_path(id_a, id_b, parents)
                    queue.append((nxt, dist + 1))
        return None

    def _materialize_path(self, id_a, id_b, parents):
        hops = []
        node = id_b
        while node != id_a:
            prev, edge = parents[node]
            hops.append((node, edge))
            node = prev
        hops.reverse()
        node_ids = [id_a] + [n for n, _ in hops]
        node_map = {n["id"]: n for n in self._nodes_by_ids(node_ids)}
        path: list[dict] = [node_map[id_a]]
        for nid, edge in hops:
            path.append(edge)
            path.append(node_map[nid])
        return path

    def list_communities(self, repo=None):
        clause, params = "", {}
        if repo:
            clause = " WHERE repo = %(repo)s"
            params["repo"] = repo
        rows = self.client.query(
            f"SELECT repo, community, count() AS node_count"
            f" FROM table({self.prefix}kg_nodes){clause}"
            " GROUP BY repo, community ORDER BY node_count DESC",
            parameters=params,
        ).result_rows
        return [dict(zip(["repo", "community", "node_count"], r)) for r in rows]

    def read_source(self, repo, file_path, line_start, line_end):
        root = self.repo_paths.get(repo)
        if root is None:
            raise ValueError(f"unknown repo {repo!r}; known: {sorted(self.repo_paths)}")
        target = (Path(root) / file_path).resolve()
        if not target.is_relative_to(Path(root).resolve()):
            raise ValueError(f"path {file_path!r} escapes repo checkout")
        lines = target.read_text(errors="replace").splitlines(keepends=True)
        start = max(line_start, 1)
        return "".join(lines[start - 1 : line_end])
