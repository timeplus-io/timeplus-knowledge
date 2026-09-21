"""LangChain tool bindings over KnowledgeGraph — thin passthroughs, mirroring
the MCP server's tool surface.

Per the spec's error-handling contract ("tool errors return structured error
strings to the LLM so it can retry or degrade"), every tool body is wrapped
so that an exception raised by the KnowledgeGraph never propagates out of
the LangGraph tool node -- it comes back as a string the model can read and
react to instead of aborting the whole run.
"""

from langchain_core.tools import tool


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # tool errors go back to the model as data
        return f"TOOL_ERROR ({type(exc).__name__}): {exc}"


def build_agent_tools(kg) -> list:
    @tool
    def search_entities(
        query: str,
        kinds: list[str] | None = None,
        repos: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict] | str:
        """Find code/doc entities in the Timeplus knowledge graph by keyword.
        Results are ranked; multi-word queries fall back to best-effort
        matching when no entity matches every word. Start here for any
        question; use kinds/repos to narrow. `limit` is capped at 200."""
        result = _safe(kg.search_entities, query, kinds=kinds, repos=repos, limit=limit)
        if result == []:
            return (
                "NO_RESULTS: nothing in the graph matches any word of "
                f"{query!r}. Try ONE different, distinctive keyword (e.g. a "
                "config name, class name, or concept). If 3-4 varied "
                "keywords all return NO_RESULTS, stop searching and answer "
                "that you could not find this in the knowledge graph."
            )
        return result

    @tool
    def get_entity(entity_id: str) -> dict | None | str:
        """Fetch the full record for one entity by its id. Returns null if
        the id does not exist."""
        return _safe(kg.get_entity, entity_id)

    @tool
    def neighbors(
        entity_id: str,
        rels: list[str] | None = None,
        direction: str = "both",
        depth: int = 1,
        confidence: str | None = None,
    ) -> dict | str:
        """Local subgraph around an entity (BFS, depth capped at 3): its
        callers, callees, containers, and related nodes. This is the tool for
        "what calls X / what does X call / who uses X" questions — get the
        entity id from search_entities first, then call neighbors(id).
        direction: out|in|both. confidence: EXTRACTED|INFERRED filters edges."""
        return _safe(
            kg.neighbors,
            entity_id, rels=rels, direction=direction, depth=depth, confidence=confidence,
        )

    @tool
    def path_between(id_a: str, id_b: str, max_depth: int = 8,
                     mode: str = "auto") -> dict | str:
        """How are two entities connected? Returns {found, mode, direction, path,
        note}. mode="auto" (default) looks for a DIRECTED CALL CHAIN first
        (A calls ... calls B, or the reverse -- `direction` says which; the
        path is listed caller first), then falls back to a "related" path
        over other relations, which is NOT a call chain -- always check
        `mode` before describing the result. mode="calls" / "related" force
        one. When found is false, `callees_of_a` / `callers_of_b` list the
        direct calls at each end: the extracted call graph is incomplete
        (C++ virtual dispatch, untyped member calls), so continue from those
        with neighbors() and read_source rather than concluding "no
        connection". max_depth is capped at 12."""
        return _safe(kg.path_between, id_a, id_b, max_depth=max_depth, mode=mode)

    @tool
    def list_communities(repo: str | None = None, limit: int | None = None,
                         min_nodes: int = 1) -> dict | str:
        """Cluster overview, largest first. BOUNDED: returns at most `limit`
        communities (default 50, max 200) with at least `min_nodes` nodes, as
        {communities, total, returned, truncated, by_repo}. Check `truncated`
        / `total`; pass `repo` (an entry key from `by_repo`) to drill into one
        repo, raise `min_nodes` to skip tiny clusters. A community id is an
        opaque number unique only within its repo -- read `top_dirs` /
        `top_files` to see what a cluster is about.
        Useful for orientation questions about a repo's structure."""
        return _safe(kg.list_communities, repo=repo, limit=limit, min_nodes=min_nodes)

    @tool
    def read_source(repo: str, file_path: str, line_start: int, line_end: int) -> str:
        """Read exact lines from a repo checkout (capped at 400 lines) so
        answers can quote real code or docs."""
        return _safe(kg.read_source, repo, file_path, line_start, line_end)

    return [search_entities, get_entity, neighbors, path_between, list_communities, read_source]
