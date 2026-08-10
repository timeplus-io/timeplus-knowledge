"""KnowledgeGraph: the single read path over the kg_* streams.

Consumed by the MCP server (this plan) and the LangGraph agent (follow-up plan).
"""

from pathlib import Path

NODE_FIELDS = [
    "id", "repo", "kind", "name", "qualified_name", "file_path",
    "line_start", "line_end", "summary", "community", "visibility",
]
EDGE_FIELDS = ["src", "dst", "rel", "confidence", "repo"]


class KnowledgeGraph:
    MAX_DEPTH = 3
    MAX_NODES_PER_HOP = 200
    # A path needs more headroom than a neighborhood-depth query -- the two
    # endpoints can be much further apart than a local BFS neighborhood.
    MAX_PATH_DEPTH = 6
    MAX_SOURCE_LINES = 400

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
        # never into the returned nodes) -- filter against the node rows we
        # actually fetched (not just `seen`) so a dangling edge whose
        # endpoint row is missing from kg_nodes (e.g. partial ingest
        # failure) can't leak into the returned edge list either.
        nodes = self._nodes_by_ids(sorted(seen))
        returned_ids = {n["id"] for n in nodes}
        edges = [e for e in all_edges.values() if e["src"] in returned_ids and e["dst"] in returned_ids]
        return {
            "nodes": nodes,
            "edges": edges,
            "depth_used": depth_used,
        }

    def path_between(self, id_a, id_b, max_depth: int = 4):
        max_depth = min(max(max_depth, 1), self.MAX_PATH_DEPTH)
        if id_a == id_b:
            return self._nodes_by_ids([id_a])
        parents: dict[str, tuple[str, dict]] = {}
        seen = {id_a}
        frontier = [id_a]
        for _ in range(max_depth):
            if not frontier:
                break
            # One batched round-trip per BFS level (IN-clause over the whole
            # frontier) instead of one query per dequeued node.
            edges = self._edges_touching(frontier, None, "both", None)
            next_frontier: set[str] = set()
            for e in edges:
                for cur, nxt in ((e["src"], e["dst"]), (e["dst"], e["src"])):
                    if nxt in seen:
                        continue
                    seen.add(nxt)
                    parents[nxt] = (cur, e)
                    next_frontier.add(nxt)
            if id_b in seen:
                return self._materialize_path(id_a, id_b, parents)
            # Same cap as `neighbors`: bound total work per level, sorted for
            # determinism.
            frontier = sorted(next_frontier)[: self.MAX_NODES_PER_HOP]
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
        start = node_map.get(id_a)
        if start is None:
            return None
        path: list[dict] = [start]
        for nid, edge in hops:
            node = node_map.get(nid)
            if node is None:
                # A dangling edge: the endpoint's node row is missing (e.g.
                # partial ingest failure). A path through a node we can't
                # present isn't presentable either.
                return None
            path.append(edge)
            path.append(node)
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
        """Read lines [line_start, line_end] (1-indexed, inclusive) from a repo
        checkout. The returned window is capped at MAX_SOURCE_LINES lines: a
        wider request is silently truncated to line_start + MAX_SOURCE_LINES - 1,
        it does not raise.
        """
        root = self.repo_paths.get(repo)
        if root is None:
            raise ValueError(f"unknown repo {repo!r}; known: {sorted(self.repo_paths)}")
        target = (Path(root) / file_path).resolve()
        if not target.is_relative_to(Path(root).resolve()):
            raise ValueError(f"path {file_path!r} escapes repo checkout")
        lines = target.read_text(errors="replace").splitlines(keepends=True)
        start = max(line_start, 1)
        end = min(line_end, start + self.MAX_SOURCE_LINES - 1)
        return "".join(lines[start - 1 : end])
