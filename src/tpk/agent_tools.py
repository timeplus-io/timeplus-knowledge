"""LangChain tool bindings over KnowledgeGraph — thin passthroughs, mirroring
the MCP server's tool surface."""

from langchain_core.tools import tool


def build_agent_tools(kg) -> list:
    @tool
    def search_entities(
        query: str,
        kinds: list[str] | None = None,
        repos: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Find code/doc entities in the Timeplus knowledge graph by keyword.
        Every whitespace-separated token must match the entity's name,
        qualified name, or summary (case-insensitive). Start here for any
        question; use kinds/repos to narrow."""
        return kg.search_entities(query, kinds=kinds, repos=repos, limit=limit)

    @tool
    def get_entity(entity_id: str) -> dict | None:
        """Fetch the full record for one entity by its id. Returns null if
        the id does not exist."""
        return kg.get_entity(entity_id)

    @tool
    def neighbors(
        entity_id: str,
        rels: list[str] | None = None,
        direction: str = "both",
        depth: int = 1,
        confidence: str | None = None,
    ) -> dict:
        """Local subgraph around an entity (BFS, depth capped at 3).
        direction: out|in|both. confidence: EXTRACTED|INFERRED filters edges."""
        return kg.neighbors(
            entity_id, rels=rels, direction=direction, depth=depth, confidence=confidence
        )

    @tool
    def path_between(id_a: str, id_b: str, max_depth: int = 4) -> list[dict] | None:
        """Shortest connection between two entities (depth capped at 6), or
        null if none found."""
        return kg.path_between(id_a, id_b, max_depth=max_depth)

    @tool
    def list_communities(repo: str | None = None) -> list[dict]:
        """Cluster overview: (repo, community, node_count), largest first.
        Useful for orientation questions about a repo's structure."""
        return kg.list_communities(repo=repo)

    @tool
    def read_source(repo: str, file_path: str, line_start: int, line_end: int) -> str:
        """Read exact lines from a repo checkout (capped at 400 lines) so
        answers can quote real code or docs."""
        return kg.read_source(repo, file_path, line_start, line_end)

    return [search_entities, get_entity, neighbors, path_between, list_communities, read_source]
