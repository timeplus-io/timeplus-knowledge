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
        Every whitespace-separated token must match the entity's name,
        qualified name, or summary (case-insensitive). Start here for any
        question; use kinds/repos to narrow."""
        return _safe(kg.search_entities, query, kinds=kinds, repos=repos, limit=limit)

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
        """Local subgraph around an entity (BFS, depth capped at 3).
        direction: out|in|both. confidence: EXTRACTED|INFERRED filters edges."""
        return _safe(
            kg.neighbors,
            entity_id, rels=rels, direction=direction, depth=depth, confidence=confidence,
        )

    @tool
    def path_between(id_a: str, id_b: str, max_depth: int = 4) -> list[dict] | None | str:
        """Shortest connection between two entities (depth capped at 6), or
        null if none found."""
        return _safe(kg.path_between, id_a, id_b, max_depth=max_depth)

    @tool
    def list_communities(repo: str | None = None) -> list[dict] | str:
        """Cluster overview: (repo, community, node_count), largest first.
        Useful for orientation questions about a repo's structure."""
        return _safe(kg.list_communities, repo=repo)

    @tool
    def read_source(repo: str, file_path: str, line_start: int, line_end: int) -> str:
        """Read exact lines from a repo checkout (capped at 400 lines) so
        answers can quote real code or docs."""
        return _safe(kg.read_source, repo, file_path, line_start, line_end)

    return [search_entities, get_entity, neighbors, path_between, list_communities, read_source]
