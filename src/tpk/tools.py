"""KnowledgeGraph: the single read path over the kg_* streams.

Consumed by the MCP server (this plan) and the LangGraph agent (follow-up plan).
"""

import threading
import time
from contextvars import ContextVar
from pathlib import Path

# Per-request cap on which corpus entry keys are queryable. `None` means
# unrestricted (admin chat, MCP server, CLI). Set/reset by the /chat
# handler for non-admin users; langchain-core's executor copies the
# contextvars context into tool threads, so tool calls observe it.
ROLE_SCOPE: ContextVar[frozenset[str] | None] = ContextVar("ROLE_SCOPE", default=None)

NODE_FIELDS = [
    "id", "repo", "kind", "name", "qualified_name", "file_path",
    "line_start", "line_end", "summary", "community", "visibility",
]
EDGE_FIELDS = ["src", "dst", "rel", "confidence", "repo"]


class _LockedRows:
    """Mimics timeplus_connect's `client.query(...)` return value (just the
    `.result_rows` attribute `tpk.corpus`'s helpers read)."""

    __slots__ = ("result_rows",)

    def __init__(self, rows: list[tuple]):
        self.result_rows = rows


class _LockedClientProxy:
    """Adapts `KnowledgeGraph._query_rows` to the `client.query(...)
    .result_rows` shape so `tpk.corpus` functions (written against a plain
    timeplus_connect client) can be reused from inside KnowledgeGraph
    without opening a second, unlocked path to `self.client`."""

    def __init__(self, kg: "KnowledgeGraph"):
        self._kg = kg

    def query(self, sql: str, parameters: dict | None = None) -> _LockedRows:
        return _LockedRows(self._kg._query_rows(sql, parameters=parameters))


class KnowledgeGraph:
    MAX_DEPTH = 3
    MAX_NODES_PER_HOP = 200
    # A path needs more headroom than a neighborhood-depth query -- the two
    # endpoints can be much further apart than a local BFS neighborhood.
    MAX_PATH_DEPTH = 6
    MAX_SOURCE_LINES = 400

    def __init__(self, client, stream_prefix: str = "",
                 repo_paths: dict[str, Path] | None = None,
                 corpus_ttl: float = 5.0):
        self.client = client
        self.prefix = stream_prefix
        self.repo_paths = repo_paths or {}
        # timeplus_connect forbids concurrent queries within one session
        # ("Attempt to execute concurrent queries within the same session").
        # KnowledgeGraph's single client is shared across LangGraph's
        # thread-pooled parallel tool calls and across concurrent /chat
        # requests (both the MCP server and the FastAPI app cache one
        # KnowledgeGraph per process), so every call into `self.client` must
        # be serialized through this lock.
        self._lock = threading.Lock()
        self._corpus_ttl = corpus_ttl
        self._corpus_cached_at = 0.0
        self._corpus_keys: list[str] | None = None
        self._corpus_paths: dict[str, Path] = {}

    # -- internals ---------------------------------------------------------

    def _query_rows(self, sql: str, parameters: dict | None = None) -> list[tuple]:
        """The single choke point for talking to `self.client` -- holds
        `self._lock` for the duration of the call so overlapping tool calls
        (parallel LangGraph tool dispatch, concurrent /chat requests, the MCP
        server) never issue concurrent queries on the shared session."""
        with self._lock:
            return self.client.query(sql, parameters=parameters).result_rows

    def _corpus_state(self) -> tuple[list[str] | None, dict[str, Path]]:
        """Enabled entry keys + their resolved paths, cached for
        `corpus_ttl` seconds. `(None, {})` means the kg_repos store is
        absent/empty (or unreadable) -> no filtering, static repo_paths
        only. An empty-but-present list (`[]`, store non-empty but every
        entry disabled) is a real "match nothing" state, distinct from
        `None` -- callers must not treat it as unfiltered."""
        now = time.monotonic()
        if self._corpus_cached_at and now - self._corpus_cached_at < self._corpus_ttl:
            return self._corpus_keys, self._corpus_paths
        keys: list[str] | None = None
        paths: dict[str, Path] = {}
        try:
            from tpk import corpus

            entries = corpus.list_entries(_LockedClientProxy(self), prefix=self.prefix)
            if entries:
                keys = corpus.enabled_keys_from(entries)
                paths = corpus.entry_paths([e for e in entries if e.enabled])
        except Exception:
            keys, paths = None, {}
        # Intentionally written without `self._lock`. This is a benign race
        # under the GIL: two threads refreshing concurrently at worst both
        # recompute and each write the (same-shaped) result -- a redundant
        # `list_entries` round-trip, never a torn read (each assignment here
        # is a single reference swap) or inconsistent data. Leave it
        # unlocked: `self._lock` is a plain (non-reentrant) `threading.Lock`,
        # and the fetch just above already goes through `_query_rows`, which
        # itself takes `self._lock` -- wrapping this whole method in
        # `self._lock` too would self-deadlock the thread that refreshes the
        # cache.
        self._corpus_keys, self._corpus_paths = keys, paths
        self._corpus_cached_at = now
        return keys, paths

    def _scoped_corpus_state(self) -> tuple[list[str] | None, dict[str, Path]]:
        """`_corpus_state()` with the per-request role scope applied.
        Semantics: corpus None + scope None -> no filter; corpus None +
        scope set -> filter to scope; corpus list + scope None -> corpus
        list; both set -> intersection (possibly empty)."""
        keys, paths = self._corpus_state()
        scope = ROLE_SCOPE.get()
        if scope is None:
            return keys, paths
        if keys is None:
            return sorted(scope), {k: p for k, p in paths.items() if k in scope}
        return ([k for k in keys if k in scope],
                {k: p for k, p in paths.items() if k in scope})

    def _nodes_by_ids(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        clauses, params = ["id IN %(ids)s"], {"ids": ids}
        active, _ = self._scoped_corpus_state()
        if active is not None:
            clauses.append("repo IN %(active_repos)s")
            params["active_repos"] = active or ["__none__"]
        rows = self._query_rows(
            f"SELECT {', '.join(NODE_FIELDS)} FROM table({self.prefix}kg_nodes)"
            f" WHERE {' AND '.join(clauses)}",
            parameters=params,
        )
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
        active, _ = self._scoped_corpus_state()
        if active is not None:
            clauses.append("repo IN %(active_repos)s")
            params["active_repos"] = active or ["__none__"]
        rows = self._query_rows(
            f"SELECT {', '.join(EDGE_FIELDS)} FROM table({self.prefix}kg_edges)"
            f" WHERE {' AND '.join(clauses)}",
            parameters=params,
        )
        return [dict(zip(EDGE_FIELDS, r)) for r in rows]

    # -- public tools ------------------------------------------------------

    def search_entities(self, query, kinds=None, repos=None, limit=20):
        tokens = [t for t in query.split() if t]
        if not tokens:
            return []
        token_exprs, params = [], {"q": query.lower(), "limit": limit}
        for i, tok in enumerate(tokens):
            params[f"t{i}"] = f"%{tok.lower()}%"
            token_exprs.append(
                f"(lower(name) LIKE %(t{i})s OR lower(qualified_name) LIKE %(t{i})s"
                f" OR lower(summary) LIKE %(t{i})s)"
            )
        filters = []
        if kinds:
            filters.append("kind IN %(kinds)s")
            params["kinds"] = kinds
        if repos:
            # Accept either the full entry key (name@ref) or the bare repo
            # name: the chat agent and Explorer UI often pass just
            # "proton-enterprise" while nodes are keyed "proton-enterprise@ref".
            # Match the full `repo` OR its name part (before the first '@'), so
            # an agent that drops the ref still filters to the right corpus.
            filters.append(
                "(repo IN %(repos)s OR splitByChar('@', repo)[1] IN %(repos)s)"
            )
            params["repos"] = repos
        active, _ = self._scoped_corpus_state()
        if active is not None:
            filters.append("repo IN %(active_repos)s")
            params["active_repos"] = active or ["__none__"]

        def _run(where_clauses, order):
            return self._query_rows(
                f"SELECT {', '.join(NODE_FIELDS)} FROM table({self.prefix}kg_nodes)"
                f" WHERE {' AND '.join(where_clauses)}"
                f" ORDER BY {order} LIMIT %(limit)s",
                parameters=params,
            )

        rows = _run(
            token_exprs + filters,
            "(lower(name) = %(q)s) DESC, length(name) ASC",
        )
        if not rows and len(tokens) > 1:
            # Strict all-tokens match found nothing; fall back to any-token
            # matching ranked by how many tokens matched, so phrase-like
            # queries still surface the closest entities.
            score = " + ".join(f"({e})" for e in token_exprs)
            params["score_min"] = 0
            rows = _run(
                [f"({score}) > %(score_min)s"] + filters,
                f"({score}) DESC, length(name) ASC",
            )
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
        clauses, params = [], {}
        if repo:
            clauses.append("repo = %(repo)s")
            params["repo"] = repo
        active, _ = self._scoped_corpus_state()
        if active is not None:
            clauses.append("repo IN %(active_repos)s")
            params["active_repos"] = active or ["__none__"]
        clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query_rows(
            f"SELECT repo, community, count() AS node_count"
            f" FROM table({self.prefix}kg_nodes){clause}"
            " GROUP BY repo, community ORDER BY node_count DESC",
            parameters=params,
        )
        return [dict(zip(["repo", "community", "node_count"], r)) for r in rows]

    @staticmethod
    def _resolve_repo_root(repo, corpus_paths, static_paths):
        """Resolve a repo argument to a checkout root, accepting either the
        full entry key (name@ref) or the bare repo name. Exact key wins
        (corpus entries take precedence over static repo_paths); a bare name
        resolves only when it matches exactly one entry key by its name part,
        so ambiguous refs (helm-charts@v13 vs @v12) return None and the caller
        raises, prompting the agent to retry with an explicit key."""
        if repo in corpus_paths:
            return corpus_paths[repo]
        if repo in static_paths:
            return static_paths[repo]
        merged = {**static_paths, **corpus_paths}
        matches = {k: v for k, v in merged.items() if k.split("@", 1)[0] == repo}
        if len(matches) == 1:
            return next(iter(matches.values()))
        return None  # unknown, or ambiguous across multiple refs

    def read_source(self, repo, file_path, line_start, line_end):
        """Read lines [line_start, line_end] (1-indexed, inclusive) from a repo
        checkout. The returned window is capped at MAX_SOURCE_LINES lines: a
        wider request is silently truncated to line_start + MAX_SOURCE_LINES - 1,
        it does not raise.
        """
        _, corpus_paths = self._scoped_corpus_state()
        scope = ROLE_SCOPE.get()
        # `corpus_paths` is already scope-filtered by `_scoped_corpus_state`,
        # but static `repo_paths` (from repos.toml, not the corpus store)
        # bypasses that filter entirely -- restrict it here too so a scoped
        # user can't read a repo outside their role via its bare name.
        static_paths = (
            self.repo_paths if scope is None
            else {k: p for k, p in self.repo_paths.items() if k in scope}
        )
        root = self._resolve_repo_root(repo, corpus_paths, static_paths)
        if root is None:
            # Include the live corpus entry keys (name@ref), not just the
            # static `repo_paths` bare names, so the LLM sees the actual
            # valid keys to retry with.
            known = sorted(set(static_paths) | set(corpus_paths))
            raise ValueError(f"unknown repo {repo!r}; known: {known}")
        target = (Path(root) / file_path).resolve()
        if not target.is_relative_to(Path(root).resolve()):
            raise ValueError(f"path {file_path!r} escapes repo checkout")
        if not target.is_file():
            raise ValueError(f"file not found in {repo}: {file_path}")
        lines = target.read_text(errors="replace").splitlines(keepends=True)
        start = max(line_start, 1)
        end = min(line_end, start + self.MAX_SOURCE_LINES - 1)
        return "".join(lines[start - 1 : end])
